"""
track_record.py — the receipts engine.

Canonical logic for proving the model works, in two forms:

  run_backtest(feat_df, weeks)   OUT-OF-TIME validation that runs today:
                                 train as-of (today − weeks), then grade the
                                 price engine's forecasts on the weeks it never
                                 saw. Returns a dict of metrics + a plain verdict.

  score_live(prior_recs, feat)   LIVE scoring for when a brand IS acting:
                                 compare a past run's recommended prices to the
                                 sales that actually followed. Accumulates real
                                 predicted-vs-actual receipts over time.

  build_track_record(feat, run)  Assembles BOTH into one payload for the Excel
                                 "Track Record" sheet (Stage 8) and the
                                 proof_loop.py CLI.

Everything is purely discount/volume based — no COGS or margin assumptions.
The "saving" is wasted discount recovered; the "reinvest" is discount
redeployed where it grows volume. See doc/legacy/MODEL.md.
"""
import os
import glob
import numpy as np
import pandas as pd
import v4_config as cfg

RECENT_DAYS = 28          # window for the current-level anchor
DEFAULT_WEEKS = 8


# ── stats helpers ─────────────────────────────────────────────────────
def _r2(a, p):
    a = np.asarray(a, float); p = np.asarray(p, float)
    m = np.isfinite(a) & np.isfinite(p)
    if m.sum() < 2:
        return np.nan
    ss_res = ((a[m] - p[m]) ** 2).sum()
    ss_tot = ((a[m] - a[m].mean()) ** 2).sum()
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan


def _mape(a, p, floor=0.5):
    a = np.asarray(a, float); p = np.asarray(p, float)
    m = np.isfinite(a) & np.isfinite(p)
    if m.sum() < 1:
        return np.nan
    return float(np.mean(np.abs((a[m] - p[m]) / np.maximum(a[m], floor))) * 100)


def _cid_series(df):
    COL = cfg.COL
    if COL["grammage"] in df.columns:
        return (df[COL["product_id"]].astype(str) + "_"
                + df[COL["grammage"]].astype(str) + "_"
                + df[COL["city"]].astype(str))
    return df[COL["product_id"]].astype(str) + "_" + df[COL["city"]].astype(str)


def _decision_pred(price, disc, base_units, base_price, base_disc, elast, badge):
    """
    Stage-5 price/badge curve — the engine that sets prices. The predicted
    log-units are clipped to [-3, 10] (units in ~[0.05, 22026]) — the SAME band
    the full-model diagnostics use (np.exp(np.clip(yhat_log, -3, 10))) — so a
    single deep-promo row whose price is far below the base can't explode the
    accuracy metric or a saving claim.
    """
    if not base_price or base_price <= 0 or price <= 0 or base_units <= 0:
        return np.nan
    log_units = (np.log(base_units) + elast * np.log(price / base_price)
                 + badge * (disc - base_disc))
    return float(np.exp(np.clip(log_units, -3, 10)))


# ── OUT-OF-TIME BACKTEST (runs today) ─────────────────────────────────
def run_backtest(feat_df, weeks=DEFAULT_WEEKS):
    """
    Train as-of (max_date − weeks), then validate the price engine on the
    held-out forward window. Returns a metrics dict (see keys below).
    """
    from stage4_model.elasticity import train_hierarchical_model
    COL = cfg.COL
    qty = COL["offtake_qty"]
    out = {"available": False, "weeks": weeks}

    if feat_df is None or feat_df.empty:
        out["reason"] = "no feature data"
        return out

    feat = feat_df.copy()
    feat[COL["date"]] = pd.to_datetime(feat[COL["date"]])
    max_date = feat[COL["date"]].max()
    cutoff = max_date - pd.Timedelta(days=weeks * 7)

    feat_train = feat[feat[COL["date"]] <= cutoff].copy()
    forward = feat[feat[COL["date"]] > cutoff].copy()
    if "is_regular_day" in forward.columns:
        forward = forward[forward["is_regular_day"] == 1]
    if feat_train.empty or forward.empty:
        out["reason"] = "not enough data on one side of the cutoff"
        return out

    try:
        result = train_hierarchical_model(feat_train)
    except Exception as e:
        out["reason"] = f"as-of training failed: {type(e).__name__}: {e}"
        return out
    elas = result["elasticities"].copy()

    coef = elas.set_index("cell_id")[
        ["price_elasticity", "badge_sensitivity", "avg_selling_price",
         "avg_units", "avg_discount_pct", "confidence_tier"]].to_dict("index")

    ft = feat_train.copy(); ft["cell_id"] = _cid_series(ft)
    if "is_regular_day" in ft.columns:
        ft = ft[ft["is_regular_day"] == 1]
    recent = ft[ft[COL["date"]] > (cutoff - pd.Timedelta(days=RECENT_DAYS))]
    rbase = recent.groupby("cell_id").agg(
        r_units=(qty, "mean"), r_price=("selling_price", "mean"),
        r_disc=("discount_pct", "mean")).to_dict("index")

    base = {}
    for cid, c in coef.items():
        rb = rbase.get(cid)
        base[cid] = {
            "elast": float(c["price_elasticity"]), "badge": float(c["badge_sensitivity"]),
            "tier": c["confidence_tier"],
            "units": rb["r_units"] if rb else c["avg_units"],
            "price": rb["r_price"] if rb else c["avg_selling_price"],
            "disc":  rb["r_disc"]  if rb else c["avg_discount_pct"],
        }

    forward["cell_id"] = _cid_series(forward)
    forward = forward[forward["cell_id"].isin(base.keys())]
    if forward.empty:
        out["reason"] = "no forward rows for learned cells"
        return out

    # (a) daily + bin forecast accuracy
    rows = []
    for _, r in forward.iterrows():
        b = base[r["cell_id"]]
        pred = _decision_pred(float(r["selling_price"]), float(r["discount_pct"]),
                              b["units"], b["price"], b["disc"], b["elast"], b["badge"])
        if not np.isfinite(pred):
            continue
        rows.append({"cell_id": r["cell_id"], "tier": b["tier"],
                     "bin": int(float(r["discount_pct"]) // 3 * 3),
                     "actual": float(r[qty]), "pred": max(float(pred), 0.01)})
    fdf = pd.DataFrame(rows)
    if len(fdf) < 5:
        out["reason"] = "too few forward predictions"
        return out

    binned = fdf.groupby(["cell_id", "bin"], as_index=False).agg(
        n=("actual", "size"), a=("actual", "mean"), p=("pred", "mean"))
    binned = binned[binned["n"] >= 3]

    # (b) discount-move validation (the money claim) + per-cell detail
    agg_kwargs = dict(fwd_disc=("discount_pct", "mean"),
                      fwd_price=("selling_price", "mean"), fwd_units=(qty, "mean"))
    if COL["city"] in forward.columns:
        agg_kwargs["city"] = (COL["city"], "first")
    if COL["title"] in forward.columns:
        agg_kwargs["title"] = (COL["title"], "first")
    fwd_cell = forward.groupby("cell_id").agg(**agg_kwargs).reset_index()
    mv = []
    for _, r in fwd_cell.iterrows():
        b = base[r["cell_id"]]
        if b["units"] < 1:
            continue
        pu = _decision_pred(r["fwd_price"], r["fwd_disc"], b["units"], b["price"],
                            b["disc"], b["elast"], b["badge"])
        if not np.isfinite(pu):
            continue
        label = str(r.get("title", r["cell_id"]))[:22]
        if "city" in r and r.get("city"):
            label = f"{label} · {r['city']}"
        mv.append({
            "cell_id": r["cell_id"], "label": label,
            "disc_move_ppt": r["fwd_disc"] - b["disc"],
            "base_price": round(float(b["price"]), 1),
            "achieved_price": round(float(r["fwd_price"]), 1),
            "base_units": round(float(b["units"]), 1),
            "pred_units": round(float(pu), 1),
            "actual_units": round(float(r["fwd_units"]), 1),
            "pred": (pu / b["units"] - 1) * 100,
            "actual": (r["fwd_units"] / b["units"] - 1) * 100,
        })
    mvdf = pd.DataFrame(mv)

    def _move(x):
        return "Discount CUT (price ↑)" if x <= -1 else (
               "Discount RAISED (price ↓)" if x >= 1 else "Held ~flat")
    move_rows, cut = [], None
    if not mvdf.empty:
        mvdf["move"] = mvdf["disc_move_ppt"].apply(_move)
        g = mvdf.groupby("move").agg(cells=("pred", "size"), pred=("pred", "mean"),
                                     actual=("actual", "mean")).reset_index()
        move_rows = g.to_dict("records")
        c = mvdf[mvdf["move"] == "Discount CUT (price ↑)"]
        if not c.empty:
            cut = {"cells": int(len(c)), "pred": float(c["pred"].mean()),
                   "actual": float(c["actual"].mean())}

    # verdict
    verdict, verdict_text = "weak", ("Too few clean price moves in the forward "
                                     "window to validate — lean on a live price test.")
    if cut:
        p, a = cut["pred"], cut["actual"]
        same_dir = (p < 0 and a < 0) or (p > 0 and a > 0) or (abs(p) < 1 and abs(a) < 1)
        if same_dir and abs(p) >= abs(a):
            verdict = "directional_conservative"
            verdict_text = (f"Direction correct and CONSERVATIVE: the engine predicted a "
                            f"{abs(p):.1f}% volume drop from raising price, but the real "
                            f"drop was only {abs(a):.1f}%. So the VOLUME RISK behind a cut "
                            f"is, if anything, overstated — pulling wasted discount back is "
                            f"safer than the tool claims. (This validates volume direction, "
                            f"not the rupee figure itself; magnitudes imprecise — quote "
                            f"ranges.)")
        elif same_dir:
            verdict = "directional_aggressive"
            verdict_text = (f"Direction correct but the engine UNDER-stated the volume hit "
                            f"(predicted {abs(p):.1f}%, actual {abs(a):.1f}%). Treat savings "
                            f"as optimistic and apply a downward margin.")

    def _fin(x, nd):
        """Round if finite, else None — never emit NaN as a 'receipt'."""
        return round(float(x), nd) if x is not None and np.isfinite(x) else None

    bin_ok = len(binned) >= 3
    out.update({
        "available": True,
        "cutoff": str(cutoff.date()),
        "max_date": str(max_date.date()),
        "n_forward_days": int(len(fdf)),
        "n_cells": int(fdf["cell_id"].nunique()),
        "daily_r2": _fin(_r2(fdf["actual"], fdf["pred"]), 3),
        "daily_mape": _fin(_mape(fdf["actual"], fdf["pred"]), 1),
        "bin_r2": _fin(_r2(binned["a"], binned["p"]), 3) if bin_ok else None,
        "bin_mape": _fin(_mape(binned["a"], binned["p"]), 1) if bin_ok else None,
        "move_table": [
            {"move": r["move"], "cells": int(r["cells"]),
             "pred": round(float(r["pred"]), 1), "actual": round(float(r["actual"]), 1)}
            for r in move_rows],
        "cut": cut,
        "verdict": verdict,
        "verdict_text": verdict_text,
    })

    # Illustrative per-cell sample for the "live" Section B: the cells whose
    # price actually moved most in the holdout, scored as if recommended.
    sample = []
    if not mvdf.empty:
        moved = mvdf[mvdf["disc_move_ppt"].abs() >= 1.0].copy()
        moved["amove"] = moved["disc_move_ppt"].abs()
        moved = moved.sort_values("amove", ascending=False).head(8)
        for _, r in moved.iterrows():
            sample.append({
                "label": r["label"],
                "base_price": r["base_price"], "achieved_price": r["achieved_price"],
                "pred_units": r["pred_units"], "actual_units": r["actual_units"],
                "pred_vol": round(float(r["pred"]), 1),
                "actual_vol": round(float(r["actual"]), 1),
            })
    out["live_sample"] = sample
    return out


# ── LIVE scoring (when a brand is acting) ─────────────────────────────
def find_prior_recommendations(current_run_dir):
    """Most recent earlier run dir (by name) that has a recommendations.csv."""
    out_root = cfg.OUTPUT_DIR
    cur = os.path.basename(os.path.normpath(current_run_dir))
    candidates = []
    for d in glob.glob(os.path.join(out_root, "*")):
        name = os.path.basename(d)
        if not os.path.isdir(d) or name.startswith("_"):
            continue
        if name >= cur:                      # only strictly-earlier runs
            continue
        recs = os.path.join(d, "recommendations.csv")
        if os.path.exists(recs):
            candidates.append((name, recs))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def score_live(prior_recs_csv, feat_df, since_date=None):
    """
    Compare a past run's recommended prices to the sales that ACTUALLY FOLLOWED.

    `since_date` (str/Timestamp) restricts the achieved-window aggregation to
    days strictly AFTER the recommendation was made — pass the prior run's
    recommendation date here in real weekly operation. Without it, the achieved
    figures average the whole history and dilute compliance/forecast-error, so
    callers scoring real outcomes should always supply it.

    Returns a per-cell DataFrame: recommended vs achieved price (did they
    comply?), predicted vs realised units, and realised discount-spend change.

    NOTE on `realised_monthly_saving`: it is measured against the prior run's
    `current_price` (the cell's pre-recommendation baseline) — i.e. "how much
    less discount spend vs. the old baseline." It is a baseline-relative figure,
    not a controlled causal saving. Empty if no overlap.
    """
    COL = cfg.COL
    qty = COL["offtake_qty"]
    if not prior_recs_csv or not os.path.exists(prior_recs_csv):
        return pd.DataFrame()
    rec = pd.read_csv(prior_recs_csv)
    if "cell_id" not in rec.columns or rec.empty:
        return pd.DataFrame()

    fresh = feat_df.copy()
    fresh["cell_id"] = _cid_series(fresh)
    if "is_regular_day" in fresh.columns:
        fresh = fresh[fresh["is_regular_day"] == 1]
    if since_date is not None and COL["date"] in fresh.columns:
        fresh = fresh[pd.to_datetime(fresh[COL["date"]]) > pd.to_datetime(since_date)]
    if fresh.empty:
        return pd.DataFrame()
    actual = fresh.groupby("cell_id").agg(
        achieved_price=("selling_price", "mean"),
        achieved_units=(qty, "mean"),
        achieved_disc=("discount_pct", "mean")).reset_index()

    out = rec.merge(actual, on="cell_id", how="inner")
    if out.empty:
        return out
    if "rec_price" in out.columns:
        out["complied"] = (out["achieved_price"] - out["rec_price"]).abs() <= (0.03 * out["rec_price"].clip(lower=1))
    if "rec_units_day" in out.columns:
        out["units_forecast_err_pct"] = (
            (out["achieved_units"] - out["rec_units_day"])
            / out["rec_units_day"].clip(lower=0.5) * 100).round(1)
    if "current_price" in out.columns:
        out["realised_monthly_saving"] = (
            (out["achieved_price"] - out["current_price"]) * out["achieved_units"] * 30.0).round(0)
    return out


# ── assemble both for the Excel sheet / CLI ───────────────────────────
def build_track_record(feat_df, current_run_dir, weeks=None):
    """
    Returns {'backtest': {...}, 'live': {...}} for the Track Record sheet.

    The BACKTEST (Section A) is the out-of-time validation. The holdout window
    is capped at ~1/3 of the data span so a short (e.g. 90-day) export still
    leaves enough training history — an 8-week holdout on 90 days would starve
    the model.

    LIVE results require a genuine "after the recommendation" period. With a
    single static dataset there is none, so we show an honest placeholder;
    score_live() activates once the dataset extends past a prior run's date.
    """
    if weeks is None:
        try:
            span = int((pd.to_datetime(feat_df[cfg.COL["date"]]).max()
                        - pd.to_datetime(feat_df[cfg.COL["date"]]).min()).days) + 1
        except Exception:
            span = 365
        weeks = min(DEFAULT_WEEKS, max(3, (span // 7) // 3))  # holdout ≤ ~1/3 of span
    backtest = run_backtest(feat_df, weeks=weeks)

    prior = find_prior_recommendations(current_run_dir)
    prior_note = ""
    if prior is not None:
        prior_run = os.path.basename(os.path.dirname(prior))
        prior_note = (f" A prior run ({prior_run}) is on file and will be the first "
                      f"baseline scored once new post-recommendation data arrives.")

    # Illustrative back-cast: the brand's OWN historical price moves in the
    # holdout, scored as if the tool had recommended them, against REAL actual
    # units. Demonstrates exactly what the live scorecard will show. Clearly
    # labelled as illustrative — it is NOT a real acted result.
    sample = (backtest or {}).get("live_sample") or []
    if sample:
        live = {
            "available": True,
            "illustrative": True,
            "cells": sample,
            "note": ("ILLUSTRATIVE back-cast — these are the brand's OWN historical price "
                     "moves over the holdout window, scored as if the tool had recommended "
                     "them, against the REAL units that occurred. It shows exactly what your "
                     "weekly live scorecard will look like. It is NOT a real acted result "
                     "(these moves weren't tool-driven); genuine receipts begin once you act "
                     "on a recommendation and a fresh week of data arrives." + prior_note),
        }
    else:
        live = {
            "available": False,
            "note": ("Live tracking begins after your first acted cycle. Once you set the "
                     "recommended prices and a fresh week of sales is added, this section "
                     "shows predicted vs. actual per city. The scoring engine (score_live) "
                     "is built and ready." + prior_note),
        }
    return {"backtest": backtest, "live": live}
