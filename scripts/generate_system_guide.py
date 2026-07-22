"""
generate_system_guide.py — build DISCOUNT_OPTIMIZER_SYSTEM_GUIDE.pdf

One self-contained, business-owner-friendly PDF: the entire workflow with
diagrams, the logic of every stage in plain language, the proof layer, and a
structured guide to reading every output. Regenerate any time the system
changes:  python -X utf8 scripts/generate_system_guide.py

NOTE ON CHARACTERS: the built-in PDF fonts (Helvetica/WinAnsi) have no glyphs
for the rupee sign, arrows, checkmarks or Greek letters — so this document uses
"Rs.", drawn arrow shapes, "phi"/"kappa" spelled out, etc. Do not "fix" that.
"""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                Table, TableStyle, KeepTogether)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "doc", "pdf", "DISCOUNT_OPTIMIZER_SYSTEM_GUIDE.pdf")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

# ── design tokens ─────────────────────────────────────────────────────
INK    = HexColor("#0F172A")
ACCENT = HexColor("#1E3A5F")
POS    = HexColor("#15803D")
NEG    = HexColor("#B91C1C")
MUTED  = HexColor("#6B7280")
FILL   = HexColor("#EFF3F8")
FILL2  = HexColor("#E2EFDA")
FILL3  = HexColor("#FCE4D6")
RULE   = HexColor("#9CA3AF")

W = 460  # drawing width

ss = getSampleStyleSheet()
def _st(name, **kw):
    kw.setdefault("fontName", "Helvetica")
    kw.setdefault("parent", ss["Normal"])
    return ParagraphStyle(name, **kw)

H1   = _st("H1", fontName="Helvetica-Bold", fontSize=17, leading=21,
           textColor=INK, spaceBefore=6, spaceAfter=8)
H2   = _st("H2", fontName="Helvetica-Bold", fontSize=12.5, leading=16,
           textColor=ACCENT, spaceBefore=10, spaceAfter=4)
BODY = _st("BODY", fontSize=9.8, leading=14, textColor=HexColor("#1F2937"),
           spaceAfter=5)
BUL  = _st("BUL", parent=BODY, leftIndent=14, bulletIndent=4, spaceAfter=3)
NOTE = _st("NOTE", fontSize=8.8, leading=12.5, textColor=MUTED, spaceAfter=5)
COVT = _st("COVT", fontName="Helvetica-Bold", fontSize=26, leading=32,
           textColor=INK, alignment=TA_CENTER)
COVS = _st("COVS", fontSize=12, leading=17, textColor=MUTED, alignment=TA_CENTER)

def B(text):  return Paragraph(text, BODY)
def N(text):  return Paragraph(text, NOTE)
def bullet(text): return Paragraph(text, BUL, bulletText="•")

def tbl(data, widths, header=True, fs=8.6):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), fs),
        ("TEXTCOLOR", (0, 0), (-1, -1), HexColor("#1F2937")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, HexColor("#E5E7EB")),
    ]
    if header:
        style += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("TEXTCOLOR", (0, 0), (-1, 0), INK),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK)]
    t.setStyle(TableStyle(style))
    return t

def P(text, style=BODY):
    return Paragraph(text, style)

# cell paragraph for tables (wraps)
CELL = _st("CELL", fontSize=8.6, leading=11.5, textColor=HexColor("#1F2937"))
CELLB = _st("CELLB", parent=CELL, fontName="Helvetica-Bold", textColor=INK)
def C(text):  return Paragraph(text, CELL)
def CB(text): return Paragraph(text, CELLB)

# ── diagram helpers ───────────────────────────────────────────────────
def _arrow_down(d, x, y1, y2, color=RULE):
    d.add(Line(x, y1, x, y2 + 5, strokeColor=color, strokeWidth=1.2))
    d.add(Polygon([x - 4, y2 + 6, x + 4, y2 + 6, x, y2], fillColor=color,
                  strokeColor=color))

def _box(d, x, y, w, h, title, subs, fill=FILL, tc=INK, stroke=RULE):
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=stroke,
               strokeWidth=0.8))
    ty = y + h - 14
    d.add(String(x + w / 2, ty, title, fontName="Helvetica-Bold",
                 fontSize=9, fillColor=tc, textAnchor="middle"))
    for s in subs:
        ty -= 11
        d.add(String(x + w / 2, ty, s, fontName="Helvetica", fontSize=7.6,
                     fillColor=MUTED, textAnchor="middle"))

def flow_diagram(steps, box_w=330):
    """Vertical flow of boxes with arrows. steps: [(title,[sub,sub],fill)]"""
    heights = [26 + 11 * len(s[1]) for s in steps]
    gap = 16
    H = sum(heights) + gap * (len(steps) - 1) + 4
    d = Drawing(W, H)
    x = (W - box_w) / 2
    y = H - 2
    for i, (title, subs, fill) in enumerate(steps):
        h = heights[i]
        y -= h
        _box(d, x, y, box_w, h, title, subs, fill=fill)
        if i < len(steps) - 1:
            _arrow_down(d, W / 2, y, y - gap)
            y -= gap
    return d

def flywheel_diagram():
    d = Drawing(W, 150)
    _box(d, 10, 96, 200, 46, "CUT side (raise prices)",
         ["cells where discount is wasted;", "walk price up to the proven floor"],
         fill=FILL2, tc=POS)
    _box(d, 250, 96, 200, 46, "REINVEST side (drop prices)",
         ["elastic cells where deeper discount", "adds REAL volume (net of leakage)"],
         fill=FILL3, tc=NEG)
    # funds arrow
    d.add(Line(210, 119, 250, 119, strokeColor=RULE, strokeWidth=1.2))
    d.add(Polygon([244, 123, 244, 115, 251, 119], fillColor=RULE, strokeColor=RULE))
    d.add(String(230, 126, "funds", fontName="Helvetica", fontSize=7.5,
                 fillColor=MUTED, textAnchor="middle"))
    _arrow_down(d, 110, 96, 62)
    _arrow_down(d, 350, 96, 62)
    _box(d, 90, 14, 280, 46, "PORTFOLIO WEIGHTED DISCOUNT",
         ["glides down week by week toward the", "target, without shocking any single city"],
         fill=FILL, tc=INK)
    return d

def leakage_diagram():
    d = Drawing(W, 120)
    d.add(String(10, 104, "A promo week's unit spike, decomposed:",
                 fontName="Helvetica-Bold", fontSize=9, fillColor=INK))
    x0, y0, bw, bh = 10, 58, 440, 26
    segs = [("REAL new demand", 0.62, FILL2, POS),
            ("BORROWED (phi)", 0.22, FILL3, NEG),
            ("STOLEN (kappa)", 0.16, FILL, ACCENT)]
    x = x0
    for label, frac, fill, tc in segs:
        w = bw * frac
        d.add(Rect(x, y0, w, bh, fillColor=fill, strokeColor=RULE, strokeWidth=0.7))
        d.add(String(x + w / 2, y0 + bh / 2 - 3, label, fontName="Helvetica-Bold",
                     fontSize=7.6, fillColor=tc, textAnchor="middle"))
        x += w
    d.add(String(10, 40, "REAL: kept.  BORROWED: post-promo dip - customers stocked up, sales taken from",
                 fontName="Helvetica", fontSize=7.8, fillColor=MUTED))
    d.add(String(10, 29, "next weeks.  STOLEN: your own sibling pack dipped while this one spiked.",
                 fontName="Helvetica", fontSize=7.8, fillColor=MUTED))
    d.add(String(10, 12, "Only REAL demand counts toward a 'deeper discount works here' decision.",
                 fontName="Helvetica-Bold", fontSize=7.8, fillColor=INK))
    return d

# ── footer ────────────────────────────────────────────────────────────
def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(50, 26, "Discount Optimizer - System Guide")
    canvas.drawRightString(A4[0] - 50, 26, f"page {doc.page}")
    canvas.restoreState()

# ── build ─────────────────────────────────────────────────────────────
def build():
    doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=50, rightMargin=50,
                            topMargin=52, bottomMargin=46,
                            title="Discount Optimizer - System Guide",
                            author="Discount Waste Recovery")
    el = []

    # ── COVER ─────────────────────────────────────────────────────────
    el.append(Spacer(1, 120))
    el.append(Paragraph("Discount Optimizer", COVT))
    el.append(Spacer(1, 10))
    el.append(Paragraph("The Complete System Guide", _st(
        "cs", fontName="Helvetica-Bold", fontSize=15, textColor=ACCENT,
        alignment=TA_CENTER)))
    el.append(Spacer(1, 26))
    el.append(Paragraph(
        "How the engine decides the right selling price for every product in "
        "every city, every week - the full workflow, the logic behind each "
        "stage, the proof it works, and how to read every output.", COVS))
    el.append(Spacer(1, 40))
    cover_pts = [
        "Finds discount that is WASTED (customers would buy anyway)",
        "Finds discount that WORKS (real, un-borrowed volume growth)",
        "Refuses to guess where the data is too thin",
        "Proves itself on data it has never seen - receipts, not promises",
    ]
    for p in cover_pts:
        el.append(Paragraph(p, _st("cv", fontSize=10.5, leading=18,
                                   textColor=INK, alignment=TA_CENTER)))
    el.append(Spacer(1, 60))
    el.append(Paragraph("Reference deployment: 4 SKUs x 11 cities on Blinkit  |  "
                        "regenerate: scripts/generate_system_guide.py", NOTE))
    el.append(PageBreak())

    # ── CONTENTS ──────────────────────────────────────────────────────
    el.append(Paragraph("Contents", H1))
    for i, s in enumerate([
        "The business problem it solves",
        "The system at a glance (workflow diagram)",
        "Stage-by-stage: what each step does and why",
        "The decision logic: cut, reinvest, hold - and the safety gates",
        "The proof layer: why the numbers can be trusted",
        "How to read the outputs (all 9 sheets + proof documents)",
        "The weekly operating rhythm",
        "Honest limits - what this system is NOT",
        "Glossary (plain-language definitions)",
    ], 1):
        el.append(Paragraph(f"<b>{i}.</b>  {s}", _st(
            "toc", fontSize=11, leading=20, textColor=HexColor("#1F2937"))))
    el.append(PageBreak())

    # ── 1. BUSINESS PROBLEM ───────────────────────────────────────────
    el.append(Paragraph("1.  The business problem it solves", H1))
    el.append(B("A CPG brand on quick-commerce spends lakhs every month on "
                "discounts. Some of that discount genuinely creates sales. A "
                "large share does not - customers would have bought at a higher "
                "price, and the difference is margin given away for nothing. "
                "The two are invisible to the naked eye because they are mixed "
                "together in the same sales numbers, and they differ by product "
                "AND by city."))
    el.append(B("<b>The question the system answers, every Monday:</b>"))
    el.append(B("<i>\"For each product, in each city, what selling price should "
                "be live this week - so we stop paying for discount customers "
                "don't notice, and redirect it to where it genuinely grows "
                "volume?\"</i>"))
    el.append(Paragraph("Reference numbers (live deployment)", H2))
    el.append(tbl([
        ["Metric", "Value"],
        ["Portfolio", "4 SKUs x 11 cities = 33 product-city cells"],
        ["Gross sales", "Rs. 78.9 L / month"],
        ["Discount spend", "Rs. 18.4 L / month  (23.3% of gross)"],
        ["Recoverable waste identified", "~ Rs. 1.76 L / month across 29 cells"],
    ], [150, 310]))
    el.append(N("Numbers regenerate on every weekly run; these are from the "
                "current live report. A 30-50 SKU brand carries proportionally "
                "larger discount spend and waste."))

    # ── 2. AT A GLANCE ────────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("2.  The system at a glance", H1))
    el.append(B("One year of daily platform data goes in; one 9-sheet Excel "
                "action plan comes out. Eight stages run in sequence - each "
                "stage's output is the next stage's input:"))
    el.append(Spacer(1, 6))
    el.append(flow_diagram([
        ("INPUT: daily sales exports (.xlsx)",
         ["one row per product x city x day: units, price,", "discount %, availability, ad share"], FILL),
        ("Stage 1 - Ingest + brand filter",
         ["combine files, keep only YOUR brand (guarded),", "auto-detect product categories from titles"], FILL),
        ("Stage 2 - Clean + flag",
         ["mark stockout days, festival/sale days, outliers -", "only NORMAL days teach the model"], FILL),
        ("Stage 3 - Build features",
         ["price, discount badge, availability, ads,", "competitor position, seasonality"], FILL),
        ("Stage 4 - Learn price sensitivity (the brain)",
         ["per category + per cell: how much do units move", "when price moves? + a confidence score per cell"], FILL2),
        ("Stage 5 - Response curves",
         ["for every cell: predicted units at every", "possible discount level 0-30%"], FILL),
        ("Stage 6 - Economics ladder",
         ["cost of each discount level vs the volume", "it buys; find where it stops paying"], FILL),
        ("Stage 7 - Guardrails + weekly action",
         ["safe target price per cell, ~3 ppt weekly steps,", "act / hold / do-not-act tiering"], FILL2),
        ("Stage 8 - The Monday report",
         ["9-sheet Excel: summary, glide path, track record,", "leakage, per-city plan, action lists"], FILL),
    ]))
    el.append(Spacer(1, 8))
    el.append(N("Around the pipeline sits a proof layer (Section 5): a readiness "
                "gate before any new brand is onboarded, and three validation "
                "documents that regenerate from code on every run."))

    # ── 3. STAGE BY STAGE ─────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("3.  Stage-by-stage: what each step does and why", H1))

    stages = [
        ("Stage 1 - Ingest and brand filter",
         ["Reads every Excel export, combines and de-duplicates them, and keeps "
          "only YOUR brand's rows (competitor rows are kept aside solely to "
          "measure your price position against them).",
          "Brand matching is guarded: if the configured brand name matches "
          "nothing, or accidentally catches a competitor's name, the run STOPS "
          "with a clear message instead of silently modelling the wrong rows.",
          "Product categories are detected automatically from product titles "
          "(brand and pack-size words stripped) - so a new brand onboards "
          "without hand-written keyword lists."]),
        ("Stage 2 - Clean and flag (why: garbage in, garbage out)",
         ["Every day is labelled: stockout day (can't sell what isn't there), "
          "festival/platform-sale day (demand spike has nothing to do with "
          "your price), or statistical outlier (a weird spike/dip).",
          "Only the remaining NORMAL days train the model. This single "
          "discipline is why the learned price sensitivity reflects price - "
          "not Diwali."]),
        ("Stage 3 - Build features (why: price is not the only lever)",
         ["For each cell and day the system assembles what else could explain "
          "sales: availability, advertising share, competitor price position, "
          "weekday/season. The model must credit those factors first - "
          "whatever remains attributable to price is the true price effect."]),
        ("Stage 4 - Learn price sensitivity (the brain)",
         ["For each category, a robust regression learns: when THIS cell's "
          "price moved, holding ads/availability/season constant, how much did "
          "units move? That slope is the ELASTICITY (e.g. -1.8 = a 1% price "
          "cut adds ~1.8% volume).",
          "Each cell gets its own elasticity, stabilised toward its category's "
          "typical value when its own history is thin - plus a 0-100 "
          "CONFIDENCE SCORE built from data depth, price variation, fit "
          "quality, plausibility, and statistical tightness.",
          "Cells scoring too low are marked DO NOT ACT. The system would "
          "rather say 'not enough evidence' than invent a number."]),
        ("Stage 5 - Response curves",
         ["Using the learned sensitivity, the system sweeps every discount "
          "level 0-30% and predicts the units each level would sell - one "
          "curve per cell. This is the menu of choices the economics stage "
          "prices out."]),
        ("Stage 6 - Economics ladder",
         ["For each discount level: what does it cost in discount spend, and "
          "what volume does it buy? Marginal logic finds the ELBOW - the depth "
          "where one more point of discount stops paying for itself."]),
        ("Stage 7 - Guardrails and the weekly action (why: safety over theory)",
         ["Target price = the cell's HISTORICAL FLOOR: the lowest discount the "
          "cell has actually operated at recently (its proven-safe level) - "
          "never a price customers have never seen.",
          "Moves glide ~3 percentage points per week, so no city gets a price "
          "shock and each week's response is observed before the next step.",
          "Every cell is tiered: STRONG CUT (clear win, act), TRADE-OFF "
          "(positive but review), HOLD, INCREASE (rare), or DO NOT ACT / "
          "NEEDS TEST (confidence too low - run a small price test first).",
          "Two extra gates: cells with weak sensitivity (inelastic, |e| <= 1) "
          "are flagged 'discount unlikely to pay - hold or raise'; and any "
          "'growth' that the leakage check shows is borrowed or stolen is "
          "removed before a cell can qualify for deeper discount."]),
        ("Stage 8 - The Monday report",
         ["Everything lands in one Excel workbook the brand team reads in ten "
          "minutes and executes in one platform session. Every number is a "
          "live formula - edit an assumption and the sheets recompute."]),
    ]
    for title, paras in stages:
        block = [Paragraph(title, H2)] + [bullet(p) for p in paras]
        el.append(KeepTogether(block))

    el.append(Spacer(1, 6))
    el.append(KeepTogether([
        Paragraph("Worked example - one cell end to end", H2),
        B("Jaggery Powder in Delhi-NCR: the model learns the cell is only "
          "mildly price-sensitive; its proven floor is a much smaller discount "
          "than it currently runs. The plan walks the price up ~3 points per "
          "week over a month to the floor, projects the small volume dip that "
          "history supports, and books the discount spend saved - visible as "
          "one row on the By Product sheet with its exact weekly prices."),
    ]))

    # ── 4. DECISION LOGIC ─────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("4.  The decision logic: cut, reinvest, hold", H1))
    el.append(B("The portfolio runs as a flywheel: money recovered from wasted "
                "discount funds the few places where deeper discount genuinely "
                "grows the business."))
    el.append(Spacer(1, 4))
    el.append(flywheel_diagram())
    el.append(Spacer(1, 8))
    el.append(Paragraph("The leakage check - real vs borrowed vs stolen", H2))
    el.append(B("Before any cell qualifies for deeper discount, its past promo "
                "bumps are decomposed. A bump is not all new demand:"))
    el.append(Spacer(1, 4))
    el.append(leakage_diagram())
    el.append(Spacer(1, 6))
    el.append(N("Observational estimates - directional signals, not controlled "
                "experiments; stated as such wherever they appear. On the "
                "reference brand, most staples showed low leakage, while "
                "Sunflower Oil showed 12-18% borrowed volume in its worst "
                "cities (edible oil stores well - customers stockpile)."))
    el.append(KeepTogether([
        Paragraph("The safety gates, in order", H2),
        tbl([
            ["Gate", "Rule", "Why"],
            [C("Confidence gate"), C("Model confidence too low -> DO NOT ACT / Needs Test"),
             C("never act on thin or contradictory data")],
            [C("Inelastic gate"), C("|elasticity| <= 1 -> 'discount unlikely to pay'"),
             C("volume response can't cover the subsidy; hold or raise")],
            [C("Leakage haircut"), C("growth counted net of borrowed + stolen units"),
             C("stockpiling and pack-swapping are not growth")],
            [C("Historical floor"), C("never target a price the cell hasn't survived"),
             C("no experiments on live revenue")],
            [C("Glide rule"), C("~3 ppt per week, observed before the next step"),
             C("no price shocks; every move is reversible")],
        ], [95, 190, 175]),
    ]))

    # ── 5. PROOF LAYER ────────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("5.  The proof layer: why the numbers can be trusted", H1))
    el.append(B("The system does not ask to be trusted - it ships three "
                "validation documents that regenerate from code on every run, "
                "plus a readiness gate for any new brand's data."))
    el.append(tbl([
        ["Proof", "What it does", "Current result"],
        [CB("Credibility report"),
         C("Separates the honest accuracy of the price engine (the part that "
           "actually sets prices) from the inflated fit of the full "
           "statistical model, on held-out data."),
         C("price-engine R-squared ~0.87 at the decision grain, ~26% error - "
           "self-rated 'Moderate', published, not hidden")],
        [CB("Track record / forward test"),
         C("Trains on old data only, then grades predictions on 8 later weeks "
           "the model never saw - including: when price actually rose, did "
           "volume fall as predicted?"),
         C("Direction correct and CONSERVATIVE: predicted -13.8% volume where "
           "reality was -8.8%. Following it is safer than it claims.")],
        [CB("Recovery test"),
         C("Plants a KNOWN price sensitivity in synthetic data with a "
           "deliberate trap (discounts co-timed with ad spikes), then checks "
           "the pipeline finds the planted answer."),
         C("Found it ~3.6x closer to truth than a naive spreadsheet fit; "
           "small honest residual (~0.2) reported, not hidden")],
        [CB("Readiness gate"),
         C("Before any new brand is onboarded: a one-page verdict on what "
           "share of their portfolio can be acted on with confidence, per "
           "product and city."),
         C("GREEN / YELLOW / RED verdict; RED = do not run production - "
           "design a price test instead")],
    ], [88, 195, 177]))
    el.append(Spacer(1, 4))
    el.append(N("Also standing guard: 32 automated tests run before any change "
                "ships, and the input validator stops a run loudly if a new "
                "data file is missing columns or unusable."))

    # ── 6. READING THE OUTPUTS ────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("6.  How to read the outputs", H1))
    el.append(B("The weekly deliverable is WASTE_REINVEST_REPORT.xlsx - nine "
                "sheets in reading order. Ten minutes, top to bottom:"))

    sheets = [
        ("1. Summary", "The portfolio in one screen: gross sales, discount "
         "spend, net revenue - today vs after the plan. Plus this week's move "
         "counts and the honest model-accuracy block.",
         "Read first. If discount spend 'after cuts' is meaningfully lower "
         "while units barely move, the week is working."),
        ("2. Glide Path", "Week-by-week projection: portfolio weighted "
         "discount, spend and cumulative savings for each coming week.",
         "Use it to see when the plan completes and what the end-state saves "
         "per month."),
        ("3. Track Record", "Part A: the forward test (trained blind, graded "
         "on unseen weeks) with its conservative verdict. Part B: predicted "
         "vs actual per city - fills with YOUR results once you act.",
         "This is the receipts sheet. Per-city rows are noisy by nature; the "
         "reliable signal is the aggregate direction in Part A."),
        ("4. Leakage", "Per cell: how much of past promo lift was real vs "
         "borrowed (phi) vs stolen (kappa), plus the 'worth discounting?' "
         "verdict (inelastic cells flagged).",
         "Cells marked 'unlikely to pay' -> hold or raise. High borrowed% -> "
         "discount that product shallower."),
        ("5. By Product (the workhorse)", "For each product: a city-by-city "
         "table - confidence, current price, target price, action, savings, "
         "and the exact selling price for every coming week (W1, W2, ...).",
         "This is what the ops team executes. Set this week's column on the "
         "platform; done."),
        ("6. Price Lifts", "Every 'raise price' move as a flat list, sorted "
         "by money wasted per month.",
         "Bulk-execution list for the platform panel; biggest savings first."),
        ("7. Price Drops", "The few strategic 'discount deeper' moves that "
         "passed every gate (elastic, real demand, budget-positive).",
         "Small list by design. If it's empty, nothing qualified - that is a "
         "finding, not a bug."),
        ("8. Needs Test", "Cells the model refuses to act on (thin or "
         "contradictory data) with what a small price test would resolve.",
         "Pick 1-2 per month for a 2-4 week mini-test; they graduate into "
         "the plan once data supports them."),
        ("9. Data (hidden)", "The raw per-cell numbers every other sheet's "
         "formulas reference.",
         "Unhide to audit any figure; edit a value to see the plan recompute."),
    ]
    rows = [["Sheet", "What it shows", "How to act on it"]]
    for name, what, act in sheets:
        rows.append([CB(name), C(what), C(act)])
    el.append(tbl(rows, [92, 195, 173]))

    el.append(Spacer(1, 8))
    el.append(KeepTogether([
        Paragraph("The proof documents (share with any skeptic)", H2),
        tbl([
            ["File", "Read it as"],
            [C("output/runs/_readiness/DATA_READINESS_REPORT.md"),
             C("'What share of this brand's portfolio can be acted on today?' "
               "- the onboarding verdict")],
            [C("output/runs/_credibility/CREDIBILITY_REPORT.md"),
             C("'How accurate is the engine that actually sets prices?' - the "
               "honest R-squared, plus bias checks")],
            [C("output/runs/_proof_loop/PROOF_LOOP_REPORT.md"),
             C("'Did its forecasts come true on weeks it never saw?' - the "
               "forward test and conservative verdict")],
            [C("output/runs/_recovery/RECOVERY_REPORT.md"),
             C("'Can it find an answer we planted?' - the machinery test a "
               "data scientist will respect")],
        ], [205, 255], fs=8.2),
        Spacer(1, 4),
        N("Also written each run: recommendations.csv (every cell's full "
          "decision detail), waste.csv / reinvest.csv (the two action lists "
          "in raw form), and outliers_removed.csv (audit trail of excluded "
          "days)."),
    ]))

    # ── 7. WEEKLY RHYTHM ─────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("7.  The weekly operating rhythm", H1))
    el.append(tbl([
        ["When", "What happens", "Who"],
        ["Monday AM", "Fresh data in; double-click run.bat; report opens", "operator"],
        ["Monday AM", "10-minute read: Summary -> Track Record -> By Product", "brand team"],
        ["Monday PM", "Strong Cut rows approved as-is; Trade-off rows reviewed", "brand team"],
        ["Tue-Wed", "Approved prices go live on the platform", "brand ops"],
        ["Thursday", "Mid-week sanity check vs prediction", "both"],
        ["Next Mon", "New data arrives; model re-learns; Track Record Part B "
                     "fills with predicted-vs-actual; cycle repeats", "-"],
    ], [70, 305, 85]))
    el.append(B("The system is self-correcting: every week's real response "
                "feeds the next week's model. Monthly, the readiness report "
                "re-runs to confirm the actionable share of the portfolio is "
                "growing."))
    el.append(KeepTogether([
        Paragraph("The handful of dials that matter (v4_config.py)", H2),
        tbl([
            ["Dial", "Default", "What it changes"],
            ["BRAND_NAME", "-", "the one line to set for a new brand"],
            ["TARGET_TIMELINE_WEEKS", "12", "how fast gaps close (8 = aggressive)"],
            ["MIN_DISCOUNT_CHANGE_PPT", "3", "smallest weekly price step"],
            ["HISTORICAL_FLOOR_PERCENTILE", "25", "how deep the proven-safe floor reaches"],
            ["INELASTIC_ELASTICITY_THRESHOLD", "1.0", "the 'discount unlikely to pay' line"],
        ], [175, 55, 230], fs=8.2),
    ]))

    # ── 8. HONEST LIMITS ─────────────────────────────────────────────
    el.append(Paragraph("8.  Honest limits - what this system is NOT", H1))
    for t in [
        "<b>Not a demand forecaster.</b> It predicts how volume responds to "
        "PRICE, not next month's total sales. Trend, festivals and demand "
        "shocks dominate absolute volume - the forward test shows this "
        "plainly, and we say it before anyone asks.",
        "<b>Not rupee-precise.</b> Savings are directionally validated ranges. "
        "The forward test shows the engine errs on the safe side - real "
        "volume loss was smaller than predicted - so quote ranges, lean on "
        "direction.",
        "<b>Not causal proof.</b> Estimates come from observed history, not "
        "randomized experiments. The remedy is built in: act, then read "
        "Track Record Part B - predicted vs actual on YOUR moves is the "
        "strongest proof there is.",
        "<b>Not fire-and-forget.</b> One platform per run, daily data "
        "required, and a human approves every move. The system recommends; "
        "people decide.",
        "<b>Not universal.</b> Cells with thin history stay behind the "
        "Needs-Test gate until a small price test earns them in. The "
        "readiness report says upfront how much of a portfolio is actionable.",
    ]:
        el.append(bullet(t))

    # ── 9. GLOSSARY ──────────────────────────────────────────────────
    el.append(PageBreak())
    el.append(Paragraph("9.  Glossary", H1))
    el.append(tbl([
        ["Term", "Plain meaning"],
        ["Cell", "one product in one city - the unit everything is decided at"],
        ["Elasticity", "how strongly units respond when price moves; -2 means a "
                       "1% price cut adds ~2% volume"],
        ["Inelastic", "elasticity weaker than 1: the volume gained can't cover "
                      "the discount given - discounting can't pay"],
        ["Historical floor", "the lowest discount a cell has actually operated at "
                             "recently - the proven-safe target"],
        ["Elbow", "the discount depth where one more point stops paying for itself"],
        ["Glide path", "the week-by-week walk from today's price to the target"],
        ["Pull-forward (phi)", "promo sales borrowed from future weeks (stock-up), "
                               "visible as the dip after the promo"],
        ["Cannibalization (kappa)", "promo sales stolen from your own other pack "
                                    "sizes of the same product"],
        ["Held-out / forward test", "grading the model on data it never saw during "
                                    "learning - the honest way to measure accuracy"],
        ["Confidence tier", "per-cell evidence score deciding whether the system "
                            "may act (High/Medium) or must wait (Low/Do-not-act)"],
        ["Needs Test", "the system's refusal to guess: run a small controlled "
                       "price change to generate the missing evidence"],
        ["Weighted discount", "portfolio discount % weighted by sales - the single "
                              "number the glide path moves down"],
    ], [130, 330], fs=8.4))
    el.append(Spacer(1, 10))
    el.append(N("Generated by scripts/generate_system_guide.py - regenerate "
                "after any system change so this document never drifts from "
                "the code."))

    doc.build(el, onFirstPage=_footer, onLaterPages=_footer)
    print(f"Saved: {OUT}")

if __name__ == "__main__":
    build()
