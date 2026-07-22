"""
params_review.py — scheduled, logged review of the OPTIMIZER'S INPUT PARAMETERS
(val_09; paper §3.3: costs, trade caps, strategic objectives and retailer/calendar
constraints get a human review every planning cycle, separate from the 4-weekly
model retrain).

What it does
------------
1. SNAPSHOTS every decision-relevant knob: the v4_config caps / glide / thresholds /
   strategic SKUs / cost assumptions, the festival-calendar version, and the runtime
   CONFIG dicts inside scripts/pricing/pricing_engine.py and de_optimizer.py (read by
   AST literal parse — no heavy imports, nothing is executed or edited).
2. DIFFS the snapshot against the last one in DISCOUNT_PLAN/params_history.json and
   logs any drift (a config edit counts as an implicit review — it resets that item's
   review clock, and the change is recorded with old -> new values).
3. Writes DISCOUNT_PLAN/PARAMS_REVIEW.md: per-item table (current value, days since
   last review, horizon, status OK/CHANGED/STALE, what-to-check question) plus the
   quarterly-review checklist.
4. `--ack --note "..."` records a human acknowledgement (resets all review clocks).

How to read it
--------------
* OK      — value unchanged and reviewed within its horizon.
* CHANGED — value differs from the last snapshot (edit = implicit review; verify it
            was intentional, the old -> new is printed).
* STALE   — unchanged AND unreviewed past its horizon (28d for the budget cap — the
            per-planning-cycle PepsiCo trade-spend analogue — 91d for the rest).
  STALE never blocks anything: this script is ADVISORY and always exits 0. It nags;
  the weekly operator decides.

Inputs :  v4_config.py (imported read-only), scripts/pricing/pricing_engine.py,
          scripts/pricing/de_optimizer.py, scripts/tracker/seasonality.py (AST-parsed),
          DISCOUNT_PLAN/tracker_history.csv (week label only).
Outputs:  DISCOUNT_PLAN/params_history.json (append-only snapshot log),
          DISCOUNT_PLAN/PARAMS_REVIEW.md (the review sheet).
Run    :  python -X utf8 scripts/tracker/params_review.py [--ack] [--note "..."]
Champion rule: reads config only; discount_plan.py / de_optimizer.py / weekly_tracker.py
are never imported or modified.
"""
import os, sys, ast, json, hashlib, argparse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import v4_config as cfg

HISTORY_JSON = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "params_history.json")
OUT_MD = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "PARAMS_REVIEW.md")
CALENDAR_MIN_RUNWAY_DAYS = 56   # calendar must cover >= 8 weeks ahead (retailer analogue)


def _ast_literal(py_path, var_name):
    """Extract a module-level literal assignment (dict/list) from a .py file WITHOUT
    importing it (pricing_engine pulls pandas/scipy at import; we only need the dict).
    Returns None if the file or variable is missing or not a pure literal."""
    try:
        tree = ast.parse(open(py_path, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == var_name:
                        return ast.literal_eval(node.value)
    except Exception:
        return None
    return None


def _calendar_snapshot():
    """Festival-calendar 'version': entry counts + latest covered date + content hash,
    across v4_config.FESTIVAL_DATES / PLATFORM_EVENT_WINDOWS and the tracker's
    seasonality.DEFAULT_FESTIVAL_CALENDAR."""
    fest = dict(getattr(cfg, "FESTIVAL_DATES", {}) or {})
    plat = {f"{a}..{b}": v for (a, b), v in (getattr(cfg, "PLATFORM_EVENT_WINDOWS", {}) or {}).items()}
    season = _ast_literal(os.path.join(ROOT, "scripts", "tracker", "seasonality.py"),
                          "DEFAULT_FESTIVAL_CALENDAR") or []
    ends = list(fest.keys()) + [k.split("..")[1] for k in plat] + \
           [e.get("end", "") for e in season if isinstance(e, dict)]
    latest = max((e for e in ends if e), default="")
    blob = repr(sorted(fest.items())) + repr(sorted(plat.items())) + repr(season)
    return {"n_config_festivals": len(fest), "n_platform_events": len(plat),
            "n_tracker_windows": len(season), "latest_covered_date": latest,
            "content_sha1": hashlib.sha1(blob.encode()).hexdigest()[:12]}


def build_register():
    """Every decision-relevant knob the optimizer chain reads, with its review horizon
    (days) and the business question a reviewer should actually answer."""
    g = lambda name: getattr(cfg, name, None)
    pe_config = _ast_literal(os.path.join(ROOT, "scripts", "pricing", "pricing_engine.py"), "CONFIG")
    de_config = _ast_literal(os.path.join(ROOT, "scripts", "pricing", "de_optimizer.py"), "DEFAULT_CONFIG")
    R = {
        # ── costs (91d; the COGS proxy also gets a PERMANENT nag below) ──────────
        "cogs_pct":            (g("DEFAULT_COGS_PCT"), 91, "Did procurement cost move? Still a 50%-of-MRP PROXY until per-SKU COGS is supplied."),
        "commission_pct":      (g("DEFAULT_COMMISSION_PCT"), 91, "Has Blinkit changed its take-rate?"),
        "fulfillment_fee_inr": (g("DEFAULT_FULFILLMENT_FEE"), 91, "Has the per-unit fulfillment fee changed?"),
        # ── caps (budget cap = 28d, the per-planning-cycle trade-spend analogue) ──
        "budget_pct_cap":      (g("DEFAULT_BUDGET_PCT_CAP"), 28, "Is 12% of gross still the agreed weekly discount-spend ceiling?"),
        "min_margin_pct":      (g("MIN_MARGIN_PCT"), 91, "Is a 5% floor above variable cost still right?"),
        "max_comp_premium":    (g("MAX_COMPETITOR_PREMIUM_PCT"), 91, "Max 10% above competitor — still the positioning rule?"),
        # ── glide (how fast discounts are allowed to move) ───────────────────────
        "use_dynamic_glide":   (g("USE_DYNAMIC_GLIDE"), 91, "Dynamic per-cell glide still wanted vs a flat cap?"),
        "target_timeline_wks": (g("TARGET_TIMELINE_WEEKS"), 91, "Is ~3 months still the agreed time to close every discount gap?"),
        "min_change_ppt":      (g("MIN_DISCOUNT_CHANGE_PPT"), 91, "Is 3ppt still the smallest customer-visible move?"),
        "use_hist_floor":      (g("USE_HISTORICAL_FLOOR_TARGET"), 91, "Glide target = historical floor (safe) vs elbow (aggressive) — still floor?"),
        "hist_floor_pctile":   (g("HISTORICAL_FLOOR_PERCENTILE"), 91, "p25 of trailing 90d as the proven-safe floor — still right?"),
        "hist_floor_lookback": (g("HISTORICAL_FLOOR_LOOKBACK_DAYS"), 91, "90-day floor lookback still representative?"),
        # ── strategic objectives ─────────────────────────────────────────────────
        "strategic_skus":      (g("STRATEGIC_SKUS"), 91, "Hero/flagship SKUs that must never be auto-cut — is the list current? (empty = no hero protection)"),
        "target_weighted_disc":(g("TARGET_WEIGHTED_DISCOUNT_PCT"), 91, "Portfolio flywheel target (9%) — still the strategy?"),
        "target_disc_pct":     (g("TARGET_DISCOUNT_PCT"), 91, "Dashboard target discount — still the goal?"),
        "target_quarter":      (g("TARGET_QUARTER"), 91, "Target quarter label — still the horizon?"),
        # ── decision thresholds ──────────────────────────────────────────────────
        "marginal_roi_thr":    (g("MARGINAL_ROI_THRESHOLD"), 91, "Elbow at marginal ROI = 1.0 — any reason to demand more?"),
        "tier_increase_roi":   (g("TIER_INCREASE_MIN_MARGINAL_ROI"), 91, "ROI > 2 = under-discounted — still the bar?"),
        "reinvest_min_lift":   (g("REINVEST_MIN_VOL_LIFT_PCT"), 91, "Reinvest needs >= 5% volume lift per +3ppt — still right?"),
        "reinvest_max_sac":    (g("REINVEST_MAX_MARGIN_SAC_PCT"), 91, "Reinvest margin sacrifice cap (10%) — still right?"),
        "reinvest_min_elast":  (g("REINVEST_MIN_ELASTICITY"), 91, "|elasticity| >= 2 to reinvest — still right?"),
        "inelastic_thr":       (g("INELASTIC_ELASTICITY_THRESHOLD"), 91, "|e| <= 1 can't pay (theorem boundary) — leave alone unless costs change."),
        "vol_drop_tolerance":  (g("VOLUME_DROP_TOLERANCE_PCT"), 91, "Kill-switch volume-drop tolerance (5%) — still the pain threshold?"),
        # ── data window (affects what the model believes) ────────────────────────
        "train_lookback_days": (g("TRAIN_LOOKBACK_DAYS"), 91, "180d training window — retune only with a backtest receipt."),
        "outlier_z":           (g("OUTLIER_Z_THRESHOLD"), 91, "Outlier z=2.0 was empirically tuned — retune only with a backtest receipt."),
        # ── calendar version (retailer-requirement analogue) ─────────────────────
        "festival_calendar":   (_calendar_snapshot(), 28, f"Does the calendar cover the next {CALENDAR_MIN_RUNWAY_DAYS//7} weeks? Add windows before they run out."),
        # ── runtime CONFIGs in the pricing chain (AST-parsed, never imported) ────
        "pricing_engine.CONFIG":      (pe_config, 28, "Optimizer run config (kpi, bounds, glide, revenue floor, psych prices) — is each edit deliberate?"),
        "de_optimizer.DEFAULT_CONFIG":(de_config, 91, "Kernel defaults (reachable-discount bounds, PPP thresholds) used by whatif/allocator."),
    }
    return R


def _hash(v):
    return hashlib.sha1(repr(v).encode()).hexdigest()[:12]


def _week_label():
    """Same convention as weekly_tracker: max week in tracker_history, else ISO week."""
    try:
        import csv
        weeks = set()
        with open(os.path.join(ROOT, "output", "DISCOUNT_PLAN", "tracker_history.csv"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                weeks.add(row.get("week", ""))
        nums = [int(w[1:]) for w in weeks if w.startswith("W") and w[1:].isdigit()]
        if nums:
            return f"W{max(nums)}"
    except Exception:
        pass
    return dt.date.today().strftime("ISO-%G-W%V")


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            return json.load(open(HISTORY_JSON, encoding="utf-8"))
        except Exception as e:
            print(f"[params_review] WARN: could not parse {HISTORY_JSON} ({e}) — starting fresh")
    return {"entries": []}


def _last_entry_with(history, item):
    """Most recent entry (snapshot or ack) that recorded this item. Returns (ts, rec)."""
    for e in reversed(history["entries"]):
        rec = e.get("params", {}).get(item)
        if rec is not None:
            return e.get("ts"), rec
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Advisory optimizer-parameter review (always exits 0).")
    ap.add_argument("--ack", action="store_true", help="record a human acknowledgement (resets all review clocks)")
    ap.add_argument("--note", default="", help="reviewer note stored with the entry")
    a = ap.parse_args()

    now = dt.datetime.now()
    reg = build_register()
    history = _load_history()
    first_run = not history["entries"]

    now_iso = now.isoformat(timespec="seconds")
    rows, drift = [], []
    for item, (value, horizon, question) in reg.items():
        h = _hash(value)
        last_ts, last_rec = _last_entry_with(history, item)
        if last_rec is None:
            status, days, reviewed = ("SEEDED" if first_run else "NEW"), 0, now_iso
        elif last_rec["hash"] != h:
            status, days, reviewed = "CHANGED", 0, now_iso   # edit = implicit review of THIS item
            drift.append((item, last_rec["value_repr"], repr(value)))
        else:
            # Per-item review clock: an unchanged item inherits its OWN reviewed_ts —
            # a drift/ack entry for some other knob must NOT reset this one's clock
            # (older entries without reviewed_ts fall back to the entry timestamp).
            reviewed = last_rec.get("reviewed_ts", last_ts)
            days = (now - dt.datetime.fromisoformat(reviewed)).days
            status = "STALE" if days > horizon else "OK"
        if a.ack:                                            # human ack = every clock resets NOW
            reviewed = now_iso
        rows.append({"item": item, "value": value, "hash": h, "horizon": horizon,
                     "status": status, "days_since": days, "question": question,
                     "last_ts": last_ts, "reviewed_ts": reviewed})

    # ── advisory nags that are independent of the review clock ──────────────────
    nags = []
    if not (os.path.exists(os.path.join(ROOT, "input_data", "cogs_per_sku.csv")) or
            os.path.exists(os.path.join(ROOT, "data", "master", "sku_costs.csv"))):
        nags.append("COGS is still the 0.50-of-MRP PROXY — per-SKU costs never supplied "
                    "(add input_data/cogs_per_sku.csv). Every profit number inherits this assumption.")
    cal = reg["festival_calendar"][0]
    if cal["latest_covered_date"]:
        runway = (dt.date.fromisoformat(cal["latest_covered_date"]) - now.date()).days
        if runway < CALENDAR_MIN_RUNWAY_DAYS:
            nags.append(f"Festival calendar runs out in {runway} days (last window ends "
                        f"{cal['latest_covered_date']}) — add next windows or festival weeks "
                        f"will be scored as waste.")
    # The PIPELINE-side calendar (v4_config.FESTIVAL_DATES feeds is_regular_day in
    # Stage 1/2) is checked separately — the tracker-side seasonality calendar can be
    # current while this one is stale, and then festival-day spikes train the model.
    cfg_fest = list(getattr(cfg, "FESTIVAL_DATES", {}) or {})
    if cfg_fest and max(cfg_fest) < now.date().isoformat():
        nags.append(f"v4_config.FESTIVAL_DATES ends {max(cfg_fest)} — the PIPELINE's event "
                    f"calendar has no festivals after that date, so festival-day demand spikes "
                    f"since then are being treated as regular days in training. Extend it.")
    if reg["pricing_engine.CONFIG"][0] is None:
        nags.append("Could not parse pricing_engine.CONFIG — file moved or no longer a literal dict.")

    # ── append a history entry when something happened (first run / drift / ack) ─
    kind = "ACK" if a.ack else ("SEED" if first_run else ("DRIFT" if drift else None))
    if kind:
        history["entries"].append({
            "ts": now.isoformat(timespec="seconds"), "week": _week_label(), "kind": kind,
            "note": a.note,
            "params": {r["item"]: {"hash": r["hash"], "value_repr": repr(r["value"]),
                                   "reviewed_ts": r["reviewed_ts"]} for r in rows},
            "drift": [{"item": i, "old": o, "new": n} for i, o, n in drift],
        })
        os.makedirs(os.path.dirname(HISTORY_JSON), exist_ok=True)
        json.dump(history, open(HISTORY_JSON, "w", encoding="utf-8"), indent=1)

    # ── PARAMS_REVIEW.md ─────────────────────────────────────────────────────────
    stale = [r for r in rows if r["status"] == "STALE"]
    L = [f"# Optimizer Parameter Review — {_week_label()} ({now.date()})\n",
         "These are the knobs the discount engine TRUSTS WITHOUT QUESTION. The model is "
         "retrained on data; these are not — a stale cap or a wrong cost assumption "
         "flows straight into every recommendation. Review cadence: budget cap & calendar "
         "every planning cycle (28d), everything else quarterly (91d).\n"]
    if drift:
        L.append("## ⚠ Changed since last snapshot (verify each edit was deliberate)\n")
        for i, o, n in drift:
            L.append(f"- **{i}**: `{o}` → `{n}`")
        L.append("")
    if nags:
        L.append("## ⚠ Standing warnings\n")
        L += [f"- {n}" for n in nags]
        L.append("")
    if stale:
        L.append(f"## ⚠ Overdue for review ({len(stale)} item(s))\n")
        for r in stale:
            L.append(f"- **{r['item']}** — {r['days_since']}d since last review (horizon {r['horizon']}d). {r['question']}")
        L.append("")
    L.append("## Full register\n")
    L.append("| item | current value | status | days since review | horizon (d) | what to check |")
    L.append("|---|---|---|---|---|---|")
    for r in rows:
        v = repr(r["value"])
        v = (v[:60] + " …") if len(v) > 60 else v
        L.append(f"| {r['item']} | `{v}` | {r['status']} | {r['days_since']} | {r['horizon']} | {r['question']} |")
    L += ["", "## Quarterly review checklist", "",
          "- [ ] COGS: confirm procurement cost per SKU (or accept the 50% proxy for another quarter — knowingly).",
          "- [ ] Commission & fulfillment: confirm Blinkit's current take-rate and per-unit fee.",
          "- [ ] Budget cap: confirm the 12% weekly discount-spend ceiling with finance.",
          "- [ ] Strategic SKUs: confirm the never-auto-cut hero list (currently "
          f"{'EMPTY — no hero protection' if not cfg.STRATEGIC_SKUS else cfg.STRATEGIC_SKUS}).",
          "- [ ] Festival calendar: covers the next 8 weeks of windows.",
          "- [ ] Glide: timeline (12 wks) and step (3 ppt) still match how fast the brand wants to move.",
          "- [ ] Thresholds: ROI / reinvest gates still match strategy (growth vs margin).",
          "- [ ] Runtime CONFIGs: any pricing_engine/de_optimizer edits were deliberate and logged.",
          "",
          f"When done: `python -X utf8 scripts/tracker/params_review.py --ack --note \"Q-review by <name>\"`",
          ""]
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

    # ── console (advisory — ALWAYS exit 0) ───────────────────────────────────────
    print("=" * 62)
    print("OPTIMIZER PARAMETER REVIEW (advisory — never blocks)")
    print("=" * 62)
    print(f"[params_review] {len(rows)} knobs snapshotted | entry: {kind or 'none (no drift, no ack)'}")
    if drift:
        print(f"[params_review] CHANGED since last snapshot: {[i for i, _, _ in drift]}")
    if stale:
        print(f"[params_review] STALE (past review horizon): {[r['item'] for r in stale]}")
    for n in nags:
        print(f"[params_review] WARN: {n}")
    if not drift and not stale and not nags:
        print("[params_review] all parameters reviewed and in-horizon — nothing to do")
    print(f"[params_review] wrote {OUT_MD}")
    print(f"[params_review] history: {HISTORY_JSON} ({len(history['entries'])} entries)")
    sys.exit(0)


if __name__ == "__main__":
    main()
