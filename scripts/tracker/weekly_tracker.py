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
sys.path.insert(0, ROOT)   # for v4_config (STRATEGIC_SKUS etc.)
sys.path.insert(0, os.path.join(ROOT, "scripts", "analysis"))
import guardrail as gr
import scorecard as sc
import seasonality as se
import workbook as wb
import actuals as ac        # GAP 1 — freeze baselines + backfill actuals
import killswitch as ks     # GAP 2 — strikes / revert / drift brake

try:
    import v4_config as _cfg
except Exception:
    _cfg = None

HISTORY = os.path.join(ROOT, "DISCOUNT_PLAN", "tracker_history.csv")
EXEC_LOG = os.path.join(ROOT, "DISCOUNT_PLAN", "execution_log.csv")
EXEC_LOG_TEMPLATE = os.path.join(ROOT, "DISCOUNT_PLAN", "execution_log_template.csv")
AGREEMENT = os.path.join(ROOT, "DISCOUNT_PLAN", "pricing", "agreement.csv")
BASELINES = os.path.join(ROOT, "DISCOUNT_PLAN", "baselines.json")
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


def _latest_fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "v4_outputs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f
    raise SystemExit("No fact_table.csv — run pipeline.py first.")


def _load_or_freeze_baselines(hist, model_panel):
    """Freeze each cell's pre-action baseline ONCE, persist it, and reuse forever
    (re-freezing a mean-reverting cell would fake wins/losses). GAP 1."""
    if os.path.exists(BASELINES):
        return json.load(open(BASELINES))
    baselines = ac.freeze_baselines(hist, model_panel) if hist is not None else {}
    json.dump(baselines, open(BASELINES, "w"), default=lambda x: None)
    return baselines


def build_plan_df(csv_path):
    """Map the model's all_cells.csv onto the shared tracker contract."""
    d = pd.read_csv(csv_path)
    # The suggested action depends ENTIRELY on the model's bucket. Only genuine
    # waste (c_waste_cut) is cut. Stock (a) / competitive (b) / monitor (f) cells
    # are HELD — never cut a cell whose flatness a confounder explains. Reinvest
    # (e) is surfaced as a test opportunity, not an automatic weekly spend increase.
    cur_disc = d["cur_disc"].astype(float)
    # STRATEGIC_SKUS are never auto-cut regardless of the math (hero/flagship items).
    try:
        import v4_config as _cfg
        strategic = set(getattr(_cfg, "STRATEGIC_SKUS", []) or [])
    except Exception:
        strategic = set()
    is_cut = (d["bucket"] == "c_waste_cut") & (~d["product_id"].isin(strategic))
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


def apply_agreement(plan_df):
    """ENGINE-AGREEMENT consumer rule (WIRING).

    DISCOUNT_PLAN/pricing/agreement.csv is PRODUCED by the pricing engine and records,
    per cell, whether BOTH engines want to lower the discount:
        columns: cell_id, product_id, city, pricing_action ('cut'|'raise'|'hold'),
                 agree_with_cut (bool)   # True iff waste-cut bucket AND optimizer also cuts

    Consumer rule enforced here: a c_waste_cut cell is only ACTUALLY cut when the
    agreement is ABSENT (backward-compatible — behave exactly as before) OR its
    agree_with_cut is True. If the file is present and agree_with_cut is False, the two
    engines disagree, so we HOLD the cell this week (suggested_disc reset to cur_disc)
    and stamp its decision_reason. Matched on (product_id, city).

    Only c_waste_cut cells are gated — stock/competitive/monitor cells are already held
    upstream, and reinvest cells are a separate deliberate test, so neither is touched.
    Returns (plan_df, n_held_for_disagreement).
    """
    if not os.path.exists(AGREEMENT):
        return plan_df, 0
    try:
        agr = pd.read_csv(AGREEMENT)
    except Exception:
        return plan_df, 0
    needed = {"product_id", "city", "agree_with_cut"}
    if not needed.issubset(agr.columns):
        return plan_df, 0

    def _truthy(v):
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1", "yes", "y", "t")

    def _pid_key(v):
        # Normalize product_id to the SAME clean string the pricing engine writes
        # (_clean_pid): strip a trailing '.0' pandas adds when the id column parses as
        # float. Without this the consumer key ('532393.0') would miss the producer's
        # cleaned key ('532393') whenever plan_df.product_id is float, silently letting a
        # disagreed cut leak through the gate.
        if isinstance(v, float):
            return str(int(v)) if float(v).is_integer() else str(v)
        s = str(v).strip()
        if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
            return s[:-2]
        return s

    agr = agr.copy()
    agr["_agree"] = agr["agree_with_cut"].map(_truthy)
    # Match key on (product_id, city) as strings, product_id normalized dtype-agnostically
    # (int/float/str all collapse to the same key the producer wrote).
    agree_map = {(_pid_key(p), str(c)): bool(a)
                 for p, c, a in zip(agr["product_id"], agr["city"], agr["_agree"])}

    df = plan_df.copy()
    is_waste_cut = df["bucket"] == "c_waste_cut"
    # A waste-cut cell whose optimizer does NOT agree with the cut -> hold it.
    disagree = []
    for idx in df.index[is_waste_cut]:
        key = (_pid_key(df.at[idx, "product_id"]), str(df.at[idx, "city"]))
        # Absent from the map -> treat as "no signal" -> do NOT block (backward-compat).
        if key in agree_map and not agree_map[key]:
            disagree.append(idx)

    if disagree:
        df.loc[disagree, "suggested_disc"] = df.loc[disagree, "cur_disc"]
        df.loc[disagree, "suggested_price"] = df.loc[disagree, "mrp"] * \
            (1 - df.loc[disagree, "suggested_disc"] / 100.0)
        # pred_units revert to current (no cut applied) so downstream deltas are honest.
        if "cur_units_wk" in df.columns and "pred_units_wk" in df.columns:
            df.loc[disagree, "pred_units_wk"] = df.loc[disagree, "cur_units_wk"]
            df.loc[disagree, "pred_net_rev_wk"] = df.loc[disagree, "pred_units_wk"] * \
                df.loc[disagree, "suggested_price"]
            df.loc[disagree, "pred_net_rev_delta_wk"] = \
                df.loc[disagree, "pred_net_rev_wk"] - df.loc[disagree, "cur_net_rev_wk"]
        df.loc[disagree, "decision_reason"] = "engines disagree - test first"
    return df, len(disagree)


def write_execution_log_template(plan_df, week_label):
    """GAP 3 (template side) — write a blank execution log for the KAM to fill.

    One row per ACTED (cut/reinvest) cell this week; 'applied' left BLANK for the KAM
    to mark Y/N and return as the real execution_log.csv (which apply_execution_log
    already reads). This is a TEMPLATE, never read back by the code.
    Columns: week, cell_id, product_id, city, recommended_action, recommended_disc, applied
    """
    if "week_action" in plan_df.columns:
        acted = plan_df[plan_df["week_action"].isin(["cut", "reinvest"])]
    else:
        acted = plan_df.iloc[:0]
    rec_disc = acted["week_disc"] if "week_disc" in acted.columns else acted.get("suggested_disc")
    tmpl = pd.DataFrame({
        "week": week_label,
        "cell_id": acted["cell_id"],
        "product_id": acted["product_id"],
        "city": acted["city"],
        "recommended_action": acted["week_action"] if "week_action" in acted.columns else "",
        "recommended_disc": rec_disc,
        "applied": "",  # KAM fills Y/N
    })
    os.makedirs(os.path.dirname(EXEC_LOG_TEMPLATE), exist_ok=True)
    tmpl.to_csv(EXEC_LOG_TEMPLATE, index=False)
    return EXEC_LOG_TEMPLATE


def _baseline_budget_pct(plan_df):
    gross = float((plan_df["cur_units_wk"] * plan_df["mrp"]).sum())
    spend = float(plan_df["cur_disc_spend_wk"].sum())
    return (spend / gross) if gross > 0 else 0.12


def append_history(plan_df, week_label, week_date):
    """Log this week's PREDICTIONS (actuals fill later, from a fresh export)."""
    cols = ["week", "week_date", "cell_id", "confidence", "scored",
            "pred_net_rev_delta", "actual_net_rev_delta", "pred_units", "actual_units", "applied"]
    hist = pd.read_csv(HISTORY) if os.path.exists(HISTORY) else pd.DataFrame(columns=cols)
    # Only cut/reinvest cells are "actions" to score; holds are logged but applied=False.
    acted = plan_df.get("week_action", pd.Series(["hold"] * len(plan_df))).isin(["cut", "reinvest"])
    new = pd.DataFrame({
        "week": week_label, "week_date": week_date, "cell_id": plan_df["cell_id"],
        "confidence": plan_df["confidence"], "scored": plan_df.get("scored", True),
        "pred_net_rev_delta": plan_df["week_saving_inr"] if "week_saving_inr" in plan_df else plan_df["pred_net_rev_delta_wk"],
        "actual_net_rev_delta": np.nan, "pred_units": plan_df.get("pred_units_wk"),
        "actual_units": np.nan,
        # applied: filled from an execution log later (GAP 3). Default False = not yet confirmed.
        "applied": False,
    })
    # store the week's action so the readout/exec-log can reference it
    new["week_action"] = plan_df.get("week_action", "hold").values if "week_action" in plan_df else "hold"
    if not ((hist["week"] == week_label).any() if len(hist) else False):
        hist = pd.concat([hist, new], ignore_index=True)
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    hist.to_csv(HISTORY, index=False)
    return hist


def apply_execution_log(hist):
    """GAP 3 — mark which recommendations were actually applied on the portal.
    Reads DISCOUNT_PLAN/execution_log.csv (columns: week, cell_id, applied[Y/N]).
    Only applied cells are scored; the rest are reported as 'not executed' (an ops
    metric, not a model miss). Absent log => nothing is scored yet (honest)."""
    if not os.path.exists(EXEC_LOG):
        return hist
    ex = pd.read_csv(EXEC_LOG)
    ex["applied_flag"] = ex["applied"].astype(str).str.strip().str.upper().isin(["Y", "YES", "TRUE", "1"])
    key = ex.set_index([ex["week"].astype(str), ex["cell_id"].astype(str)])["applied_flag"].to_dict()
    hist["applied"] = [key.get((str(w), str(c)), a) for w, c, a in
                       zip(hist["week"], hist["cell_id"], hist.get("applied", False))]
    return hist


def write_readout(plan_df, gsum, score, season, week_label, alerts=None):
    cuts = plan_df[plan_df.get("week_action", "") == "cut"].sort_values("week_saving_inr", ascending=False) \
        if "week_action" in plan_df else plan_df.iloc[:0]
    L = [f"# Weekly Discount Readout — {week_label}\n"]
    # GAP 2 — reverts / alerts FIRST, so the owner sees safety issues before new cuts.
    alerts = alerts or {}
    if alerts.get("reverts") or alerts.get("block_new_cuts"):
        L.append("## ⚠️ REVERT / ALERT — read first\n")
        if alerts.get("block_new_cuts"):
            L.append(f"- **Drift brake ON** — recent predictions are missing too often "
                     f"(hit rate {(alerts.get('hit_rate') or 0)*100:.0f}%). NEW cuts are blocked this week; "
                     f"existing holds continue. Model needs a retrain.")
        if alerts.get("reverts"):
            L.append(f"- **{len(alerts['reverts'])} cells auto-REVERTED** (lost sales 2 weeks running): "
                     f"put their discount back to the prior level — the model was wrong on these. "
                     f"Cells: {', '.join(map(str, alerts['reverts'][:12]))}"
                     + (" …" if len(alerts['reverts']) > 12 else ""))
        if alerts.get("confounded"):
            L.append(f"- {len(alerts['confounded'])} weeks were *confounded* (stock-out/visibility drop) — "
                     f"NOT counted against the model; re-read once availability recovers.")
        L.append("")
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


def _auto_week_label(prev_hist):
    """GAP 6 — derive the next week label from history instead of a human typing W1/W2."""
    if prev_hist is None or not len(prev_hist):
        return "W1"
    nums = pd.to_numeric(prev_hist["week"].astype(str).str.extract(r"W(\d+)")[0], errors="coerce").dropna()
    return f"W{int(nums.max()) + 1}" if len(nums) else "W1"


def _ks_config():
    """Kill-switch thresholds, sourced from v4_config (the values nothing read before)."""
    tol = getattr(_cfg, "VOLUME_DROP_TOLERANCE_PCT", 5.0) / 100.0 if _cfg else 0.05
    return {"vol_tol_pct": tol, "confounder_pct": 0.10, "strikes_to_revert": 2,
            "freeze_weeks": 4, "drift_min_cells": 30, "hit_rate_floor": 0.60}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default=None, help="auto-derived from history if omitted (GAP 6)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--budget_pct", type=float, default=None,
                    help="discount-spend cap as fraction of gross; default = current baseline")
    ap.add_argument("--actuals", default=None,
                    help="path to a fresh export's fact_table.csv; fills last weeks' actuals (GAP 1)")
    a = ap.parse_args()

    plan_df = build_plan_df(_latest_plan_csv())

    # ── WIRING — engine agreement: hold any waste-cut cell the pricing optimizer does
    # NOT also want to cut (agreement.csv absent => behave as before). Applied BEFORE the
    # guardrail so a disagreed cell (suggested_disc reset to cur_disc) glides to a hold.
    plan_df, n_disagree = apply_agreement(plan_df)
    if n_disagree:
        print(f"[tracker] ENGINE DISAGREEMENT — {n_disagree} waste-cut cell(s) HELD "
              f"(optimizer did not confirm the cut); test first.")

    # ── GAP 1/2 — fill prior weeks' actuals from a fresh export, then run the kill-switch ──
    prev = pd.read_csv(HISTORY) if os.path.exists(HISTORY) else None
    alerts = {}
    if a.actuals and os.path.exists(a.actuals):
        model_panel = ac.panel_from_fact_table(_latest_fact_table())   # pre-action reference
        fresh_panel = ac.panel_from_fact_table(a.actuals)
        baselines = _load_or_freeze_baselines(prev, model_panel)
        hist0 = ac.backfill_actuals(prev, fresh_panel, baselines) if prev is not None else prev
        if hist0 is not None:
            hist0, alerts = ks.evaluate(hist0, _ks_config())
            hist0.to_csv(HISTORY, index=False)
            prev = hist0

    week = a.week or _auto_week_label(prev)
    date = a.date or "2026-07-06"
    cap = a.budget_pct if a.budget_pct is not None else _baseline_budget_pct(plan_df)
    # GAP 2 — if the portfolio drift brake tripped, block NEW cuts this week
    if alerts.get("block_new_cuts"):
        plan_df.loc[plan_df["bucket"] == "c_waste_cut", "suggested_disc"] = plan_df["cur_disc"]
        print("[tracker] DRIFT BRAKE ON — new cuts blocked this week (hit-rate below floor).")
    config = {"budget_pct_cap": round(cap, 4), "max_step_ppt": MAX_STEP_PPT,
              "festival_uplift_pct": FESTIVAL_UPLIFT_PCT, "week_date": date, "week_label": week}
    print(f"[tracker] {week} {date} | {len(plan_df)} cells | budget cap {cap*100:.1f}% of gross")

    plan_df, season = se.apply_seasonality(plan_df, config)
    plan_df, gsum = gr.apply_guardrail(plan_df, config)
    # (B) Write a blank execution-log TEMPLATE for the KAM — one row per acted cell.
    tmpl_path = write_execution_log_template(plan_df, week)
    n_acted = int(plan_df["week_action"].isin(["cut", "reinvest"]).sum()) \
        if "week_action" in plan_df.columns else 0
    print(f"[tracker] wrote execution-log template ({n_acted} acted cells) -> {tmpl_path}")
    hist = append_history(plan_df, week, date)
    hist = apply_execution_log(hist)                       # GAP 3 — only applied cells count
    hist.to_csv(HISTORY, index=False)
    # score ONLY applied cells with actuals (GAP 3): unapplied = ops metric, not model miss
    scored_hist = hist[hist["actual_net_rev_delta"].notna() & hist.get("applied", False).astype(bool)] \
        if len(hist) else hist
    score = sc.score_history(scored_hist)

    wb.build_workbook(plan_df, gsum, score, season, OUT_XLSX, week)
    write_readout(plan_df, gsum, score, season, week, alerts)
    print(f"[tracker] status {gsum.get('status')} | cut {gsum.get('n_cut')} hold {gsum.get('n_hold')} "
          f"reinvest {gsum.get('n_reinvest')} | proj saving ₹{gsum.get('projected_week_saving_inr',0):,.0f}/wk")
    if alerts.get("reverts"):
        print(f"[tracker] REVERTS: {len(alerts['reverts'])} cells lost sales 2wks — discount restored.")
    print(f"[tracker] scored cells: {len(scored_hist)} | wrote {OUT_XLSX} + readout")


if __name__ == "__main__":
    main()
