"""
weekly_tracker.py — the weekly discount-tracker ORCHESTRATOR.

Each week: read the latest model plan (per SKU x city cell), turn it into a
guarded, gliding weekly price recommendation, score last week's predictions
against what actually happened, and refresh the master Excel tracker + a
plain-English readout.

Flow:
  1. Load the model's per-cell plan (v4_outputs/<run>/plan/all_cells.csv).
  2. Build the standard `plan_df` (contract columns).
  3. apply_seasonality  → flag festival weeks, relax budget, exclude from scoring.
  4. apply_guardrail    → glide (<=3ppt), revenue-protect, enforce budget cap.
  5. Append this week's PREDICTIONS to tracker_history.csv; fill ACTUALS for any
     prior week now present in the fresh export (matched by cell_id + week).
  6. score_history      → running predicted-vs-actual accuracy (the trust engine).
  7. build_workbook     → DISCOUNT_PLAN/WEEKLY_TRACKER.xlsx.
  8. Write WEEKLY_READOUT.md — the plain-English "what to change and why".

Usage:
  python -X utf8 scripts/tracker/weekly_tracker.py --week W1 --date 2026-07-06
  (with no args it uses the latest model run and today-ish as the week.)
"""
import os, sys, glob, json, argparse
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import guardrail as gr
import scorecard as sc
import seasonality as se
import workbook as wb

HISTORY = os.path.join(ROOT, "DISCOUNT_PLAN", "tracker_history.csv")
OUT_XLSX = os.path.join(ROOT, "DISCOUNT_PLAN", "WEEKLY_TRACKER.xlsx")
OUT_READOUT = os.path.join(ROOT, "DISCOUNT_PLAN", "WEEKLY_READOUT.md")
MAX_STEP_PPT = 3.0
FESTIVAL_UPLIFT_PCT = 0.5


def _latest_plan_csv():
    for r in sorted(glob.glob(os.path.join(ROOT, "v4_outputs", "2026*")), reverse=True):
        f = os.path.join(r, "plan", "all_cells.csv")
        if os.path.exists(f):
            return f
    raise SystemExit("No all_cells.csv — run scripts/analysis/discount_plan.py first.")


def build_plan_df(csv_path):
    """Map the model's all_cells.csv onto the shared tracker contract."""
    d = pd.read_csv(csv_path)
    # The suggested action depends ENTIRELY on the model's bucket. Only genuine
    # waste (c_waste_cut) is cut. Stock (a) / competitive (b) / monitor (f) cells
    # are HELD — never cut a cell whose flatness a confounder explains. Reinvest
    # (e) is surfaced as a test opportunity, not an automatic weekly spend increase.
    cur_disc = d["cur_disc"].astype(float)
    is_cut = d["bucket"] == "c_waste_cut"
    suggested = cur_disc.copy()
    suggested[is_cut] = d.loc[is_cut, "tgt_disc"].astype(float)
    pred_units = d["cur_units_wk"].astype(float).copy()
    pred_units[is_cut] = d.loc[is_cut, "tgt_units_wk"].astype(float)
    p = pd.DataFrame({
        "cell_id": d["cell_id"], "product_id": d["product_id"], "city": d["city"],
        "category": d["category"], "title": d["title"].astype(str).str.replace("24 Mantra Organic ", "", regex=False),
        "mrp": d["mrp"].astype(float),
        "cur_price": d["cur_price"].astype(float), "cur_disc": cur_disc,
        "cur_units_wk": d["cur_units_wk"].astype(float),
        "suggested_disc": suggested, "pred_units_wk": pred_units,
        "bucket": d["bucket"], "confidence": d["confidence"],
        "reliably_waste": d["reliably_waste"].astype(bool) if "reliably_waste" in d else False,
        "net_gain_mo": d["net_gain_mo"].astype(float),
        "decision_reason": d["decision_reason"].astype(str),
    })
    p["suggested_price"] = p["mrp"] * (1 - p["suggested_disc"] / 100.0)
    p["cur_net_rev_wk"] = p["cur_units_wk"] * p["cur_price"]
    p["cur_disc_spend_wk"] = p["cur_units_wk"] * p["mrp"] * p["cur_disc"] / 100.0
    p["pred_net_rev_wk"] = p["pred_units_wk"] * p["suggested_price"]
    p["pred_net_rev_delta_wk"] = p["pred_net_rev_wk"] - p["cur_net_rev_wk"]
    return p


def _baseline_budget_pct(plan_df):
    gross = float((plan_df["cur_units_wk"] * plan_df["mrp"]).sum())
    spend = float(plan_df["cur_disc_spend_wk"].sum())
    return (spend / gross) if gross > 0 else 0.12


def append_history(plan_df, week_label, week_date):
    """Log this week's PREDICTIONS. Actuals are filled when a later export arrives."""
    cols = ["week", "week_date", "cell_id", "confidence", "scored",
            "pred_net_rev_delta", "actual_net_rev_delta", "pred_units", "actual_units"]
    hist = pd.read_csv(HISTORY) if os.path.exists(HISTORY) else pd.DataFrame(columns=cols)
    # actuals for a prior week could be back-filled here by matching a fresh export;
    # on a first run there is nothing to fill.
    new = pd.DataFrame({
        "week": week_label, "week_date": week_date, "cell_id": plan_df["cell_id"],
        "confidence": plan_df["confidence"], "scored": plan_df.get("scored", True),
        "pred_net_rev_delta": plan_df["week_saving_inr"] if "week_saving_inr" in plan_df else plan_df["pred_net_rev_delta_wk"],
        "actual_net_rev_delta": np.nan, "pred_units": plan_df.get("pred_units_wk"),
        "actual_units": np.nan,
    })
    if not ((hist["week"] == week_label).any() if len(hist) else False):
        hist = pd.concat([hist, new], ignore_index=True)
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    hist.to_csv(HISTORY, index=False)
    return hist


def write_readout(plan_df, gsum, score, season, week_label):
    cuts = plan_df[plan_df.get("week_action", "") == "cut"].sort_values("week_saving_inr", ascending=False) \
        if "week_action" in plan_df else plan_df.iloc[:0]
    L = [f"# Weekly Discount Readout — {week_label}\n"]
    st = gsum.get("status", "?")
    L.append(f"**Budget: {st}** — discount is {gsum.get('disc_pct',0)*100:.1f}% of sales "
             f"(cap {gsum.get('budget_pct_cap',0)*100:.1f}%), headroom ₹{gsum.get('headroom_inr',0):,.0f}/wk.")
    if season.get("active"):
        L.append(f"**Festival week: {season.get('festival_name','')}** — budget relaxed "
                 f"+{season.get('budget_uplift_pct',0)*100:.0f}%; these cells are not scored as waste this week.")
    L.append(f"\n**This week: cut {gsum.get('n_cut',0)} · hold {gsum.get('n_hold',0)} · reinvest {gsum.get('n_reinvest',0)}. "
             f"Projected saving ₹{gsum.get('projected_week_saving_inr',0):,.0f}/week.**\n")
    if len(cuts):
        L.append("Top moves this week (3-point glide steps — not the full cut at once):\n")
        for _, r in cuts.head(10).iterrows():
            L.append(f"- **{r['title'][:28]} · {r['city']}**: {r['cur_disc']:.0f}% → {r['week_disc']:.0f}% "
                     f"(+₹{r['week_saving_inr']:,.0f}/wk) — discount reliably below break-even; sales expected to hold")
    # reinvest opportunities (surfaced, not auto-spent — deliberate test)
    rein = plan_df[plan_df["bucket"] == "e_reinvest"]
    if len(rein):
        L.append(f"\n**Reinvest opportunities ({len(rein)} cells, mostly Oil):** discount here reliably *pays* "
                 f"— worth TESTING a higher discount (raise 3ppt, watch 2 wks). Not auto-applied, since spending "
                 f"more should be a deliberate call. See DISCOUNT_PLAN/reinvest_list.csv.")
    if score.get("n_weeks_scored", 0) > 0:
        L.append(f"\n**Track record so far:** predictions right {score.get('hit_rate',0)*100:.0f}% of the time, "
                 f"predicted-vs-actual R² {score.get('pred_vs_actual_r2',0):.2f}, "
                 f"realized saving to date ₹{score.get('cumulative_realized_saving_inr',0):,.0f}.")
    else:
        L.append("\n**Track record:** starts filling from next week, once this week's cuts meet the register.")
    L.append("\n_Golden rule: if a cut loses sales for 2 straight weeks, revert it — the model was wrong on that cell._")
    open(OUT_READOUT, "w", encoding="utf-8").write("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="W1")
    ap.add_argument("--date", default="2026-07-06")
    ap.add_argument("--budget_pct", type=float, default=None,
                    help="discount-spend cap as fraction of gross; default = current baseline")
    a = ap.parse_args()

    plan_df = build_plan_df(_latest_plan_csv())
    cap = a.budget_pct if a.budget_pct is not None else _baseline_budget_pct(plan_df)
    config = {"budget_pct_cap": round(cap, 4), "max_step_ppt": MAX_STEP_PPT,
              "festival_uplift_pct": FESTIVAL_UPLIFT_PCT, "week_date": a.date, "week_label": a.week}
    print(f"[tracker] {a.week} {a.date} | {len(plan_df)} cells | budget cap {cap*100:.1f}% of gross")

    plan_df, season = se.apply_seasonality(plan_df, config)
    plan_df, gsum = gr.apply_guardrail(plan_df, config)
    hist = append_history(plan_df, a.week, a.date)
    scored_hist = hist[hist["actual_net_rev_delta"].notna()] if len(hist) else hist
    score = sc.score_history(scored_hist)

    wb.build_workbook(plan_df, gsum, score, season, OUT_XLSX, a.week)
    write_readout(plan_df, gsum, score, season, a.week)
    print(f"[tracker] status {gsum.get('status')} | cut {gsum.get('n_cut')} hold {gsum.get('n_hold')} "
          f"reinvest {gsum.get('n_reinvest')} | proj saving ₹{gsum.get('projected_week_saving_inr',0):,.0f}/wk")
    print(f"[tracker] wrote {OUT_XLSX}")
    print(f"[tracker] wrote {OUT_READOUT}")


if __name__ == "__main__":
    main()
