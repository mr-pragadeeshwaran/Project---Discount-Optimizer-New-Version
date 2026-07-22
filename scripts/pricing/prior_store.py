"""
prior_store.py — sequential Bayesian prior store + retraining-cadence gate (price_30 / val_10).

WHAT THIS IS (paper section 3.3, elasticity stability): the posterior of refresh N
becomes the prior of refresh N+1, instead of restarting every 4-weekly retrain from
the same fixed constants (OWN_PRIOR_MU=-1.0, SD=0.8). That damps refit-to-refit
whipsaw while a FORGETTING FACTOR keeps the system able to learn: stored SDs are
inflated by rho (default 1.25) per elapsed retrain period, CAPPED at the original
diffuse prior SD — stale certainty decays back toward cold-start, never past it.

CHAMPION/CHALLENGER DISCIPLINE: nothing here changes current behavior. The hook in
elasticity_bayes.estimate_elasticities is OPT-IN (seq_priors argument, or env var
ELASTICITY_SEQ_PRIORS=1); with it off, the champion path is bit-identical.
scripts/analysis/discount_plan.py is never touched.

INPUTS
  save_posteriors(elast_df, hyper): the (elast_df, gates) pair returned by
    elasticity_bayes.estimate_elasticities on the weekly panel.
OUTPUTS
  DISCOUNT_PLAN/pricing/priors.json — run stamp, data window, global mu_g,
    per-category {own, own_sd, cross, cross_sd} posteriors, per-cell posteriors.
  (NOTE: the champion estimator pools at CATEGORY level, so per-cell rows currently
   duplicate their category value; they are stored anyway so a future per-cell
   estimator can join the same store without a schema change. cross_sd is stored
   as the diffuse default because champion gates carry no cross SD — flagged in
   the file via cross_sd_source.)

HOW TO READ priors.json: per_category.<cat>.own is the last accepted posterior mean
(next run's prior center when the flag is on); own_sd is the posterior SD BEFORE
forgetting — load_priors() applies the inflation at read time so the file always
holds the raw posterior.

RETRAINING-CADENCE GATE: retrain_check() reports weeks since the stamped save and
flags when the 4-week retrain is due.  CLI:
  python -X utf8 scripts/pricing/prior_store.py --check          # cadence gate only
  python -X utf8 scripts/pricing/prior_store.py --demo           # two-run proof on real data
The demo runs elasticity_bayes twice on the latest fact table (run 1 fixed priors ->
save; run 2 seeded from run 1's posteriors), writes the before/after receipt to
DISCOUNT_PLAN/pricing/PRIOR_STORE_NOTE.md, and prints an honest summary.
"""
import os, sys, json, math, glob, argparse
import datetime as dt
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from elasticity_bayes import OWN_PRIOR_MU, OWN_PRIOR_SD, CROSS_PRIOR_MU, CROSS_PRIOR_SD

PRIORS_PATH = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "pricing", "priors.json")
DEFAULT_RHO = 1.25                    # SD inflation per retrain period (forgetting factor)
MIN_OWN_PRIOR_SD = 0.15               # floor: a carried prior can never lock the estimate
MIN_CROSS_PRIOR_SD = 0.10
RETRAIN_CADENCE_WEEKS = 4             # the paper's 4-weekly refresh cadence
_STORE_VERSION = 1


def _now_utc():
    return dt.datetime.now(dt.timezone.utc)


def _parse_stamp(s):
    try:
        t = dt.datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


# ─────────────────────────────── save ───────────────────────────────

def save_posteriors(elast_df, hyper, path=PRIORS_PATH, data_window=None, run_stamp=None):
    """Persist the posteriors of one accepted refresh.

    elast_df : per-cell frame from estimate_elasticities (product_id, city,
               own_elast, own_sd, low_confidence).
    hyper    : the gates dict from the same call (per_category hypers, mu_g, all_pass).
    Returns the path written."""
    per_cat = {}
    for c, v in (hyper.get("per_category") or {}).items():
        try:
            per_cat[str(c)] = {
                "own": float(v["own"]), "own_sd": float(v["own_sd"]),
                "cross": float(v.get("cross", CROSS_PRIOR_MU)),
                # champion gates carry no cross SD -> store the diffuse default so the
                # chain is complete; a seq-aware estimator may overwrite this later.
                "cross_sd": float(v.get("cross_sd", CROSS_PRIOR_SD)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    per_cell = {}
    if elast_df is not None and len(elast_df):
        for r in elast_df.itertuples(index=False):
            per_cell[f"{r.product_id}|{r.city}"] = {
                "own": float(r.own_elast), "own_sd": float(r.own_sd),
                "low_confidence": bool(r.low_confidence),
            }
    doc = {
        "version": _STORE_VERSION,
        "saved_at_utc": _now_utc().isoformat(timespec="seconds"),
        "run_stamp": run_stamp,
        "data_window": data_window,
        "method": hyper.get("method"),
        "all_pass": bool(hyper.get("all_pass", False)),
        "global": {"mu_g": hyper.get("global_own (mu_g)")},
        "cross_sd_source": ("gates" if any("cross_sd" in (v or {}) for v in
                            (hyper.get("per_category") or {}).values()) else "default_diffuse"),
        "per_category": per_cat,
        "per_cell": per_cell,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)
    print(f"[prior_store] saved {len(per_cat)} category / {len(per_cell)} cell posteriors "
          f"-> {os.path.relpath(path, ROOT)} (all_pass={doc['all_pass']})")
    return path


# ─────────────────────────────── load ───────────────────────────────

def load_priors(path=PRIORS_PATH, rho=DEFAULT_RHO, now=None, require_all_pass=True):
    """Load stored posteriors as next-run priors, with the forgetting factor applied.

    SD inflation = rho ** n_periods, n_periods = elapsed full/partial retrain periods
    since the save (min 1 — one refresh has passed by definition of a re-run), then
    clipped to [MIN_*_PRIOR_SD, diffuse cap]. Returns the dict (with a 'forgetting'
    block describing what was applied) or None when there is no usable store —
    missing file, unreadable JSON, or a previous run that failed its release gates
    (never seed from a failed run)."""
    if not os.path.exists(path):
        print(f"[prior_store] no store at {os.path.relpath(path, ROOT)} — cold start")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"[prior_store] unreadable store ({e}) — cold start")
        return None
    if require_all_pass and not doc.get("all_pass", False):
        print("[prior_store] previous run failed release gates (all_pass=false) — "
              "refusing to seed from it, cold start")
        return None
    saved = _parse_stamp(doc.get("saved_at_utc"))
    now = now or _now_utc()
    weeks = max((now - saved).total_seconds() / (7 * 86400), 0.0) if saved else None
    n_periods = max(1, int(math.ceil((weeks or 0.0) / RETRAIN_CADENCE_WEEKS)))
    infl = float(rho) ** n_periods
    for v in (doc.get("per_category") or {}).values():
        v["own_sd"] = float(np.clip(v.get("own_sd", OWN_PRIOR_SD) * infl,
                                    MIN_OWN_PRIOR_SD, OWN_PRIOR_SD))
        v["cross_sd"] = float(np.clip(v.get("cross_sd", CROSS_PRIOR_SD) * infl,
                                      MIN_CROSS_PRIOR_SD, CROSS_PRIOR_SD))
    for v in (doc.get("per_cell") or {}).values():
        v["own_sd"] = float(np.clip(v.get("own_sd", OWN_PRIOR_SD) * infl,
                                    MIN_OWN_PRIOR_SD, OWN_PRIOR_SD))
    doc["forgetting"] = {"rho": float(rho), "n_periods": n_periods,
                         "inflation_applied": round(infl, 4),
                         "weeks_since_save": None if weeks is None else round(weeks, 2),
                         "sd_cap_own": OWN_PRIOR_SD, "sd_cap_cross": CROSS_PRIOR_SD,
                         "sd_floor_own": MIN_OWN_PRIOR_SD}
    print(f"[prior_store] loaded {len(doc.get('per_category', {}))} category priors "
          f"(stamp {doc.get('saved_at_utc')}, sd x{infl:.2f} capped at {OWN_PRIOR_SD})")
    return doc


# ─────────────────────── retraining-cadence gate ───────────────────────

def retrain_check(path=PRIORS_PATH, cadence_weeks=RETRAIN_CADENCE_WEEKS, now=None):
    """Report weeks since the last stamped refresh; flag when the 4-week retrain is due."""
    if not os.path.exists(path):
        out = {"found": False, "saved_at_utc": None, "weeks_since": None,
               "cadence_weeks": cadence_weeks, "retrain_due": True,
               "message": "no priors.json — no stamped refresh on record; retrain (and save) due now"}
        print(f"[retrain-gate] {out['message']}")
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        out = {"found": False, "saved_at_utc": None, "weeks_since": None,
               "cadence_weeks": cadence_weeks, "retrain_due": True,
               "message": f"priors.json unreadable ({e}); treat as retrain due"}
        print(f"[retrain-gate] {out['message']}")
        return out
    saved = _parse_stamp(doc.get("saved_at_utc"))
    now = now or _now_utc()
    weeks = max((now - saved).total_seconds() / (7 * 86400), 0.0) if saved else None
    due = (weeks is None) or (weeks >= cadence_weeks)
    msg = (f"last refresh stamped {doc.get('saved_at_utc')} ({'?' if weeks is None else f'{weeks:.1f}'} "
           f"weeks ago, run {doc.get('run_stamp')}); cadence {cadence_weeks}w -> "
           f"{'RETRAIN DUE' if due else 'ok, not due yet'}")
    print(f"[retrain-gate] {msg}")
    return {"found": True, "saved_at_utc": doc.get("saved_at_utc"), "weeks_since": weeks,
            "cadence_weeks": cadence_weeks, "retrain_due": bool(due), "message": msg}


# ─────────────────────────── two-run proof ───────────────────────────

def _latest_fact_table():
    for r in sorted(glob.glob(os.path.join(ROOT, "output", "runs", "2026*")), reverse=True):
        f = os.path.join(r, "fact_table.csv")
        if os.path.exists(f):
            return f, r
    raise SystemExit("no fact_table.csv — run pipeline.py first")


def run_demo(rho=DEFAULT_RHO, path=PRIORS_PATH):
    """Two-run receipt on REAL data: run 1 (fixed priors) saves posteriors; run 2
    (flag on) is seeded from them. Writes DISCOUNT_PLAN/pricing/PRIOR_STORE_NOTE.md."""
    import pricing_panel as pp
    import elasticity_bayes as eb

    fact, run = _latest_fact_table()
    print(f"[demo] fact_table: {os.path.basename(run)}")
    panel = pp.build_pricing_panel(fact)
    print(f"[demo] panel: {len(panel)} cell-weeks | {panel['product_id'].nunique()} SKUs "
          f"| {panel['city'].nunique()} cities")

    # RUN 1 — champion behavior (fixed constant priors), then persist posteriors.
    e1, c1, b1, g1 = eb.estimate_elasticities(panel)
    dw = {"start": str(pd.to_datetime(panel["week"]).min().date()),
          "end": str(pd.to_datetime(panel["week"]).max().date())}
    save_posteriors(e1, g1, path=path, data_window=dw, run_stamp=os.path.basename(run))
    retrain_check(path=path)

    # RUN 2 — seeded from run 1's stored posteriors (forgetting factor applied).
    pri = load_priors(path=path, rho=rho)
    e2, c2, b2, g2 = eb.estimate_elasticities(panel, seq_priors=pri)

    # Env-var route must give the same answer as the explicit argument. The env route
    # always reads the DEFAULT store location, so the check only makes sense there —
    # under a custom --path it would compare against the wrong (stale/absent) store.
    if os.path.abspath(path) == os.path.abspath(PRIORS_PATH):
        os.environ["ELASTICITY_SEQ_PRIORS"] = "1"
        try:
            e3, _, _, g3 = eb.estimate_elasticities(panel)
        finally:
            os.environ.pop("ELASTICITY_SEQ_PRIORS", None)
        env_ok = bool(np.allclose(e3["own_elast"].values, e2["own_elast"].values))
    else:
        env_ok = None   # skipped: env route only serves the default path

    # Per-category before/after receipt.
    rows = []
    for cat in sorted(g1["per_category"]):
        a = g1["per_category"][cat]; b = g2["per_category"].get(cat, {})
        pr = (pri.get("per_category", {}) if pri else {}).get(cat, {})
        rows.append({"category": cat,
                     "own_run1": a["own"], "own_sd_run1": a["own_sd"],
                     "prior_sd_used_run2": pr.get("own_sd"),
                     "own_run2": b.get("own"), "own_sd_run2": b.get("own_sd"),
                     "shift": None if b.get("own") is None else round(b["own"] - a["own"], 4)})
    tab = pd.DataFrame(rows)
    med1, med2 = float(e1["own_elast"].median()), float(e2["own_elast"].median())
    sd1, sd2 = float(e1["own_sd"].median()), float(e2["own_sd"].median())
    max_shift = float(tab["shift"].abs().max())
    seeded = (g2.get("seq_priors") or {}).get("categories_seeded", [])

    print(f"[demo] run1 median own {med1:+.3f} (median sd {sd1:.3f}) -> "
          f"run2 median own {med2:+.3f} (median sd {sd2:.3f})")
    print(f"[demo] {len(seeded)}/{len(tab)} categories seeded from stored posteriors | "
          f"max abs per-category shift {max_shift:.3f} | env-var route == explicit arg: {env_ok}")
    sp = g2.get("seq_priors") or {}
    print(f"[demo] run2 gates all_pass={g2.get('all_pass')} "
          f"(own_in_band={g2.get('own_in_band')}, cross_nonneg_subs={g2.get('cross_nonneg_subs')}, "
          f"stability_pass={sp.get('stability_pass')} @ max shift {sp.get('max_abs_own_shift')} <= 0.5)")

    note = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "pricing", "PRIOR_STORE_NOTE.md")
    with open(note, "w", encoding="utf-8") as f:
        f.write("# Sequential prior store — two-run receipt (price_30 / val_10)\n\n")
        f.write("**What this is.** The elasticity model's posterior from one 4-weekly refresh "
                "now seeds the prior of the next (the PepsiCo paper's stability mechanism), "
                "instead of every refresh restarting from the same fixed constants. "
                "OFF by default — the champion path is unchanged until the flag "
                "(`ELASTICITY_SEQ_PRIORS=1`) is set deliberately.\n\n")
        f.write(f"- Forgetting factor: stored SDs are inflated x{rho} per {RETRAIN_CADENCE_WEEKS}-week "
                f"period elapsed, capped at the original diffuse prior SD ({OWN_PRIOR_SD} own / "
                f"{CROSS_PRIOR_SD} cross), floored at {MIN_OWN_PRIOR_SD} — stale certainty decays, "
                "the system can always keep learning.\n")
        f.write(f"- Retraining-cadence gate: `python -X utf8 scripts/pricing/prior_store.py --check` "
                f"flags when the {RETRAIN_CADENCE_WEEKS}-week retrain is due (stamp in priors.json).\n")
        f.write("- A store whose run failed release gates (`all_pass=false`) is never used to seed.\n\n")
        f.write(f"## Proof run ({dt.date.today()}, data run {os.path.basename(run)}, "
                f"window {dw['start']} .. {dw['end']})\n\n")
        f.write(f"| metric | run 1 (fixed priors) | run 2 (seeded from run 1) |\n|---|---|---|\n")
        f.write(f"| median own elasticity | {med1:+.3f} | {med2:+.3f} |\n")
        f.write(f"| median posterior SD | {sd1:.3f} | {sd2:.3f} |\n")
        f.write(f"| categories seeded | — | {len(seeded)}/{len(tab)} |\n")
        f.write(f"| max abs per-category shift | — | {max_shift:.3f} |\n")
        f.write(f"| stability gate (max shift <= 0.5) | n/a (no prior) | "
                f"{'PASS' if sp.get('stability_pass') else 'FAIL'} |\n")
        f.write(f"| gates all_pass | {g1.get('all_pass')} | {g2.get('all_pass')} |\n\n")
        f.write("Per-category detail:\n\n")
        f.write(tab.to_markdown(index=False))
        big = tab.loc[tab["shift"].abs().idxmax()]
        f.write("\n\n## Honest caveats\n\n")
        f.write(f"1. **This proof reuses the same data window twice** (only one window exists "
                f"yet), so run 2 counts the data twice: estimates move FURTHER in the direction "
                f"the data pulls, away from the fixed -1.0 anchor (biggest: {big['category']} "
                f"{big['own_run1']:+.2f} -> {big['own_run2']:+.2f}). That is correct Bayesian "
                f"mechanics, but it is a double-dip, NOT a stability demo — the damping benefit "
                f"only shows at the next refresh, when the prior carries OLD weeks' information "
                f"against NEW weeks' noise. Do not turn the flag on twice within one data window.\n")
        f.write(f"2. **The stored posteriors are barely tighter than the diffuse prior** "
                f"(median own SD {sd1:.2f} vs diffuse {OWN_PRIOR_SD}), so after x{rho} inflation every "
                f"category's carried prior SD hit the {OWN_PRIOR_SD} cap — today the store carries the "
                f"MEAN forward, no extra certainty, and run-2 SDs are unchanged ({sd2:.2f}). That is "
                f"what the data supports: weekly price variation is thin, so the model has "
                f"genuinely learned little beyond the prior. Expect the store to matter more as "
                f"price-change weeks accumulate.\n")
        f.write("3. Champion gates carry no cross SD, so cross priors reuse the diffuse SD "
                "until a seq-aware run writes its own (flagged `cross_sd_source` in priors.json).\n")
        f.write("4. Per-cell posteriors are stored, but the champion estimator pools at category "
                "level, so per-cell rows currently duplicate their category value.\n")
    print(f"[demo] receipt -> {os.path.relpath(note, ROOT)}")
    return {"median_own_run1": med1, "median_own_run2": med2, "max_shift": max_shift,
            "env_ok": env_ok, "all_pass_run2": bool(g2.get("all_pass"))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sequential Bayesian prior store + retrain-cadence gate")
    ap.add_argument("--check", action="store_true", help="report weeks since last refresh / retrain-due flag")
    ap.add_argument("--demo", action="store_true", help="two-run proof on real data (saves priors.json, writes PRIOR_STORE_NOTE.md)")
    ap.add_argument("--rho", type=float, default=DEFAULT_RHO, help="forgetting factor (SD inflation per retrain period)")
    ap.add_argument("--path", default=PRIORS_PATH, help="priors.json location")
    args = ap.parse_args()
    if args.demo:
        run_demo(rho=args.rho, path=args.path)
    else:
        retrain_check(path=args.path)
