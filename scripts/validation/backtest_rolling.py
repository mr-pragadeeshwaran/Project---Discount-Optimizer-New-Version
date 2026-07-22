"""
backtest_rolling.py — rolling-origin backtest of the PRODUCTION champion
(scripts/analysis/discount_plan.py), champion/challenger style: the champion is
IMPORTED read-only (same importlib pattern as scripts/analysis/challenger.py)
and never edited.

What it does (paper §3.3 analog — validate the DEPLOYED model, not a lab spec)
------------------------------------------------------------------------------
1. Builds the champion's own weekly product x city panel (dp.build_panel on the
   newest output/runs/2026*/fact_table.csv).
2. Walk-forward: >= 4 origins (default 5, 2-week spacing). At each origin the
   champion's EXACT per-category formula
       log1p(units) ~ C(cell_id) + disc + disc_sq + log_osa + log_adsov
                      + comp_share + lag1_lu + lag2_lu
   is re-fit (Huber RLM, OLS fallback — same as dp.fit_models; C(month) dropped
   exactly as dp.holdout_r2 drops it, because held-out weeks can contain an
   unseen month) on data up to the origin, then scores the NEXT `horizon` weeks.
   Two champion variants are scored:
     * champion_1step     — realized lagged units fed at prediction time
                            (identical to the champion's own holdout_r2 method;
                            flattered because week-2..4 lags peek at actuals).
     * champion_recursive — the honest 4-week-ahead forecast: predicted units
                            are fed back as the lag for the following week.
3. Benchmarks (the paper's "legacy plans" analog), same forward cell-weeks:
     * seasonal_naive     — same cell, same ISO week-of-year from a PRIOR year
                            if it exists in training, else the cell's last-4-
                            training-week mean units (with ~6 months of data the
                            fallback is what actually fires — reported).
     * naive_lastweek     — carry the cell's last training-week units forward.
4. Metrics per fold and pooled, on identical cell-week rows for every model:
   MAPE (rows with units>0), wMAPE, bias (mean pct error), R2 in log1p space.
5. Verdict: does the honest champion (recursive) beat BOTH benchmarks on pooled
   wMAPE? Reported either way.

Outputs
-------
DISCOUNT_PLAN/validation/backtest_folds.csv   one row per fold x model + POOLED
DISCOUNT_PLAN/validation/BACKTEST_REPORT.md   fold table, pooled verdict, how-to-read

How to read
-----------
wMAPE = volume-weighted absolute error (big cells count more) — the headline.
bias > 0 means over-forecast. r2_log is fit quality in log space (the scale the
model is trained on). The champion earns its keep only if champion_recursive
wMAPE < both benchmarks; champion_1step is shown as the upper bound the
production holdout measures.

Run:  python -X utf8 scripts/validation/backtest_rolling.py
      [--n-origins 5] [--step-weeks 2] [--horizon-weeks 4] [--max-minutes 8]
"""
import os, sys, glob, json, time, argparse, inspect, warnings, importlib.util
warnings.simplefilter("ignore")
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import statsmodels.api as sm
import statsmodels.formula.api as smf

# champion imported read-only — the importlib pattern used by challenger.py
_spec = importlib.util.spec_from_file_location(
    "dp", os.path.join(ROOT, "scripts", "analysis", "discount_plan.py"))
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

OUT_DIR = os.path.join(ROOT, "output", "DISCOUNT_PLAN", "validation")

# ── tunables ────────────────────────────────────────────────────────────────
MIN_TRAIN_WEEKS = 12     # skip origins with a thinner training window
MIN_CAT_ROWS    = 40     # same thin-category rule as dp.fit_models
MIN_CAT_CELLS   = 2
LOG_CLIP        = (-3.0, 15.0)   # sanity clip on predicted log1p(units)
MAX_ORIGINS_HARD = 12    # runtime guard: never sweep more than this many folds
PARTIAL_WEEK_MIN_DAYS = 5  # trailing weeks with fewer median days are dropped:
                           # the fact table can end mid-week, and grading a full-
                           # week forecast against 2 days of actuals inflates
                           # every model's error (~200% wMAPE artifact).

# Must match dp.fit_models' base formula (checked against source at runtime).
CHAMPION_FORMULA = ("np.log1p(units) ~ C(cell_id) + disc + disc_sq + log_osa "
                    "+ log_adsov + comp_share + lag1_lu + lag2_lu")


def _check_formula_drift():
    """Warn loudly if the champion's base formula ever drifts from ours."""
    src = inspect.getsource(dp.fit_models)
    core = CHAMPION_FORMULA.split("~")[1].strip()
    if core.replace(" ", "") not in src.replace(" ", ""):
        print("[backtest] WARNING: champion formula in discount_plan.py no longer "
              "matches CHAMPION_FORMULA here — results may not reflect production.")


# ── metrics ─────────────────────────────────────────────────────────────────
def _metrics(actual, pred):
    """MAPE / wMAPE / bias (mean pct error) / R2 in log1p space."""
    a = np.asarray(actual, float); p = np.asarray(pred, float)
    ok = np.isfinite(a) & np.isfinite(p)
    a, p = a[ok], np.clip(p[ok], 0, None)
    n = len(a)
    if n == 0:
        return dict(n=0, mape=np.nan, wmape=np.nan, bias_mpe=np.nan, r2_log=np.nan)
    pos = a > 0
    pe = (p[pos] - a[pos]) / a[pos]
    mape  = float(np.mean(np.abs(pe))) if pos.any() else np.nan
    wmape = float(np.sum(np.abs(p - a)) / np.sum(a)) if a.sum() > 0 else np.nan
    bias  = float(np.mean(pe)) if pos.any() else np.nan
    la, lp = np.log1p(a), np.log1p(p)
    sst = float(np.sum((la - la.mean()) ** 2))
    r2  = 1.0 - float(np.sum((la - lp) ** 2)) / sst if sst > 0 else np.nan
    return dict(n=n, mape=mape, wmape=wmape, bias_mpe=bias, r2_log=r2)


# ── champion re-fit + forward scoring for one fold ──────────────────────────
def _fit_categories(train):
    """One RLM (OLS fallback) per category on the training slice — mirrors
    dp.fit_models' estimator and thin-data rules; returns {cat: fitted_model}."""
    models = {}
    for cat, sub in train.groupby("category"):
        if len(sub) < MIN_CAT_ROWS or sub["cell_id"].nunique() < MIN_CAT_CELLS:
            continue
        try:
            models[cat] = smf.rlm(CHAMPION_FORMULA, data=sub,
                                  M=sm.robust.norms.HuberT()).fit()
        except Exception:
            try:
                models[cat] = smf.ols(CHAMPION_FORMULA, data=sub).fit()
            except Exception:
                pass
    return models


def _predict_1step(models, test):
    """Predict every test row with REALIZED lags (dp.holdout_r2's method)."""
    out = pd.Series(np.nan, index=test.index)
    for cat, m in models.items():
        sub = test[test["category"] == cat]
        if len(sub) == 0:
            continue
        try:
            yh = m.predict(sub)
            out.loc[sub.index] = np.clip(yh.values, *LOG_CLIP)
        except Exception:
            pass
    return np.expm1(out)


def _predict_recursive(models, train, test, test_weeks):
    """Honest multi-step forecast: walk the horizon week by week, feeding each
    cell's PREDICTED log-units back in as lag1/lag2 for its next appearance
    (positional lags — same convention as dp.build_panel's shift())."""
    state = {}   # cell_id -> (lag1_lu, lag2_lu)
    for cid, g in train.groupby("cell_id"):
        lu = np.log1p(g.sort_values("week")["units"].values)
        if len(lu) >= 2:
            state[cid] = (float(lu[-1]), float(lu[-2]))
    out = pd.Series(np.nan, index=test.index)
    for wk in test_weeks:
        rows = test[test["week"] == wk]
        if len(rows) == 0:
            continue
        rows = rows[rows["cell_id"].isin(state)].copy()
        if len(rows) == 0:
            continue
        rows["lag1_lu"] = rows["cell_id"].map(lambda c: state[c][0])
        rows["lag2_lu"] = rows["cell_id"].map(lambda c: state[c][1])
        for cat, m in models.items():
            sub = rows[rows["category"] == cat]
            if len(sub) == 0:
                continue
            try:
                yh = np.clip(m.predict(sub).values, *LOG_CLIP)
                out.loc[sub.index] = np.expm1(yh)
            except Exception:
                yh = np.full(len(sub), np.nan)
            # advance each cell's lag chain: predicted if finite, else realized
            for cid, pred_lu, real_u in zip(sub["cell_id"], yh, sub["units"]):
                new = pred_lu if np.isfinite(pred_lu) else np.log1p(max(real_u, 0))
                state[cid] = (float(new), state[cid][0])
        # cells whose category had no model still advance on realized units
        for cid, real_u in zip(rows["cell_id"], rows["units"]):
            g_cat = rows.loc[rows["cell_id"] == cid, "category"].iloc[0]
            if g_cat not in models:
                state[cid] = (float(np.log1p(max(real_u, 0))), state[cid][0])
    return out


# ── benchmarks ───────────────────────────────────────────────────────────────
def _benchmarks(train, test):
    """seasonal_naive + naive_lastweek per test row. Returns (seas, lastwk,
    n_true_seasonal_matches)."""
    tr = train.sort_values("week")
    last_u  = tr.groupby("cell_id")["units"].last()
    last4_u = tr.groupby("cell_id")["units"].apply(lambda s: s.tail(4).mean())
    iso = tr["week"].dt.isocalendar()
    tr = tr.assign(_woy=iso["week"].values, _yr=iso["year"].values)
    seas_lut = tr.groupby(["cell_id", "_woy"])["units"].mean()  # per (cell, week-of-year)

    lastwk = test["cell_id"].map(last_u)
    ti = test["week"].dt.isocalendar()
    seas_vals, n_seasonal = [], 0
    for cid, woy, yr in zip(test["cell_id"], ti["week"].values, ti["year"].values):
        # true seasonal match = same week-of-year from a PRIOR year in training
        hit = tr[(tr["cell_id"] == cid) & (tr["_woy"] == woy) & (tr["_yr"] < yr)]
        if len(hit):
            seas_vals.append(float(hit["units"].mean())); n_seasonal += 1
        else:
            seas_vals.append(float(last4_u.get(cid, np.nan)))
    return pd.Series(seas_vals, index=test.index), lastwk, n_seasonal


# ── driver ───────────────────────────────────────────────────────────────────
MODELS = ["champion_recursive", "champion_1step", "seasonal_naive", "naive_lastweek"]


def run_backtest(n_origins=5, step_weeks=2, horizon_weeks=4, max_minutes=8.0):
    t0 = time.monotonic()
    _check_formula_drift()
    run, fact = dp._latest_facttable()
    print(f"[backtest] fact_table: {os.path.basename(run)}")
    panel = dp.build_panel(fact)
    panel["week"] = pd.to_datetime(panel["week"])
    # drop TRAILING partial weeks (fact table ends mid-week): scoring a full-week
    # forecast against 2 days of actuals punishes every model with a fake ~200%
    # over-forecast. Lags look backward, so trimming the tail leaves them valid.
    partial_notes = []
    wk_days = panel.groupby("week")["n_days"].median().sort_index()
    while len(wk_days) and wk_days.iloc[-1] < PARTIAL_WEEK_MIN_DAYS:
        partial_notes.append(f"dropped trailing partial week "
                             f"{pd.Timestamp(wk_days.index[-1]).date()} "
                             f"(median {wk_days.iloc[-1]:.0f} days of data)")
        wk_days = wk_days.iloc[:-1]
    if partial_notes:
        panel = panel[panel["week"].isin(wk_days.index)].copy()
        for s in partial_notes:
            print(f"[backtest] {s}")
    pl = panel.dropna(subset=["lag1_lu", "lag2_lu"]).copy()  # same drop as champion
    wks = sorted(panel["week"].unique())
    print(f"[backtest] panel: {len(panel)} cell-weeks | {panel['cell_id'].nunique()} cells | "
          f"{len(wks)} weeks {pd.Timestamp(wks[0]).date()}..{pd.Timestamp(wks[-1]).date()}")

    fold_rows, pooled_frames, skipped = [], [], list(partial_notes)
    n_origins = min(int(n_origins), MAX_ORIGINS_HARD)
    for k in range(n_origins):
        oi = len(wks) - 1 - horizon_weeks - k * step_weeks
        if oi + 1 < MIN_TRAIN_WEEKS:
            skipped.append(f"fold {k}: only {oi+1} training weeks (<{MIN_TRAIN_WEEKS})")
            break
        if (time.monotonic() - t0) / 60.0 > max_minutes:
            skipped.append(f"fold {k}: runtime budget {max_minutes} min exhausted")
            break
        origin = wks[oi]
        test_weeks = wks[oi + 1: oi + 1 + horizon_weeks]
        train = pl[pl["week"] <= origin]
        # test rows: champion needs realized lags for the 1-step variant, and a
        # cell must have >=2 training rows so every model can predict it.
        cells_ok = train["cell_id"].value_counts()
        cells_ok = set(cells_ok[cells_ok >= 2].index)
        test = pl[(pl["week"] > origin) & (pl["week"] <= test_weeks[-1])]
        test = test[test["cell_id"].isin(cells_ok)].copy()
        n_test_all = int(pl[(pl["week"] > origin) & (pl["week"] <= test_weeks[-1])].shape[0])
        if len(test) < 20:
            skipped.append(f"fold {k}: only {len(test)} scoreable test rows"); continue

        models = _fit_categories(train)
        p1 = _predict_1step(models, test)
        pr = _predict_recursive(models, train, test, test_weeks)
        ps, plw, n_seas = _benchmarks(train, test)

        fold = pd.DataFrame({
            "cell_id": test["cell_id"], "week": test["week"], "actual": test["units"],
            "champion_1step": p1, "champion_recursive": pr,
            "seasonal_naive": ps, "naive_lastweek": plw,
        })
        # apples-to-apples: score only rows where EVERY model produced a number
        fold = fold.dropna(subset=MODELS)
        if len(fold) < 20:
            skipped.append(f"fold {k}: only {len(fold)} rows after aligning models"); continue
        fold["fold"] = k
        pooled_frames.append(fold)

        for mname in MODELS:
            met = _metrics(fold["actual"], fold[mname])
            fold_rows.append(dict(
                fold=k, origin_week=str(pd.Timestamp(origin).date()),
                test_start=str(pd.Timestamp(test_weeks[0]).date()),
                test_end=str(pd.Timestamp(test_weeks[-1]).date()),
                train_weeks=oi + 1, model=mname,
                n_cellweeks=met["n"], coverage=round(met["n"] / max(n_test_all, 1), 3),
                mape=round(met["mape"], 4), wmape=round(met["wmape"], 4),
                bias_mpe=round(met["bias_mpe"], 4), r2_log=round(met["r2_log"], 4),
                n_true_seasonal_matches=n_seas if mname == "seasonal_naive" else "",
                n_categories_fit=len(models),
            ))
        cm = _metrics(fold["actual"], fold["champion_recursive"])
        print(f"[backtest] fold {k}: origin {pd.Timestamp(origin).date()} "
              f"train {oi+1}wk -> test {len(fold)} cell-weeks | champion(recursive) "
              f"wMAPE {cm['wmape']:.3f} | seasonal-naive true matches: {n_seas}")

    if not pooled_frames:
        print("[backtest] FAIL: no scoreable folds — not enough data for a rolling backtest.")
        for s in skipped:
            print(f"    {s}")
        return None

    pooled = pd.concat(pooled_frames, ignore_index=True)
    pooled_stats = {m: _metrics(pooled["actual"], pooled[m]) for m in MODELS}
    for mname in MODELS:
        met = pooled_stats[mname]
        fold_rows.append(dict(
            fold="POOLED", origin_week="", test_start="", test_end="",
            train_weeks="", model=mname, n_cellweeks=met["n"], coverage="",
            mape=round(met["mape"], 4), wmape=round(met["wmape"], 4),
            bias_mpe=round(met["bias_mpe"], 4), r2_log=round(met["r2_log"], 4),
            n_true_seasonal_matches="", n_categories_fit="",
        ))
    folds_df = pd.DataFrame(fold_rows)

    # ── verdict on the HONEST champion (recursive lags) ──
    cw  = pooled_stats["champion_recursive"]["wmape"]
    sw  = pooled_stats["seasonal_naive"]["wmape"]
    lw  = pooled_stats["naive_lastweek"]["wmape"]
    beats_seasonal = bool(np.isfinite(cw) and np.isfinite(sw) and cw < sw)
    beats_lastweek = bool(np.isfinite(cw) and np.isfinite(lw) and cw < lw)
    verdict_pass = beats_seasonal and beats_lastweek
    n_folds = pooled["fold"].nunique()
    # per-fold win count for stability readout
    wins = 0
    for k, g in pooled.groupby("fold"):
        c = _metrics(g["actual"], g["champion_recursive"])["wmape"]
        s = _metrics(g["actual"], g["seasonal_naive"])["wmape"]
        l = _metrics(g["actual"], g["naive_lastweek"])["wmape"]
        wins += int(c < s and c < l)

    os.makedirs(OUT_DIR, exist_ok=True)
    folds_csv = os.path.join(OUT_DIR, "backtest_folds.csv")
    folds_df.to_csv(folds_csv, index=False)
    _write_report(folds_df, pooled_stats, verdict_pass, beats_seasonal, beats_lastweek,
                  wins, n_folds, skipped, n_origins, horizon_weeks, step_weeks,
                  elapsed_min=(time.monotonic() - t0) / 60.0)

    print(f"\n[backtest] POOLED wMAPE — champion(recursive) {cw:.3f} | "
          f"champion(1-step) {pooled_stats['champion_1step']['wmape']:.3f} | "
          f"seasonal-naive {sw:.3f} | last-week {lw:.3f}")
    print(f"[backtest] VERDICT: champion beats both benchmarks on pooled wMAPE: "
          f"{'YES' if verdict_pass else 'NO'} "
          f"(vs seasonal: {'win' if beats_seasonal else 'LOSS'}, "
          f"vs last-week: {'win' if beats_lastweek else 'LOSS'}) | "
          f"fold wins {wins}/{n_folds}")
    for s in skipped:
        print(f"[backtest] note: {s}")
    print(f"[backtest] outputs -> {folds_csv}")
    return folds_df


def _write_report(folds_df, pooled_stats, verdict_pass, beats_seasonal, beats_lastweek,
                  wins, n_folds, skipped, n_origins, horizon, step, elapsed_min):
    def _fmt(v, pct=False):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return "n/a"
        return f"{v*100:.1f}%" if pct else f"{v:.3f}"

    lines = []
    lines.append("# Rolling-Origin Backtest — Champion vs Naive Benchmarks\n")
    lines.append(f"_Generated by scripts/validation/backtest_rolling.py | "
                 f"{n_folds} folds x {horizon}-week horizon, {step}-week spacing | "
                 f"runtime {elapsed_min:.1f} min_\n")
    lines.append("## Bottom line\n")
    if verdict_pass:
        lines.append(f"**PASS — the production model beats both naive benchmarks on pooled "
                     f"wMAPE** ({_fmt(pooled_stats['champion_recursive']['wmape'])} vs "
                     f"seasonal-naive {_fmt(pooled_stats['seasonal_naive']['wmape'])} and "
                     f"last-week {_fmt(pooled_stats['naive_lastweek']['wmape'])}), "
                     f"winning {wins}/{n_folds} individual folds.\n")
    else:
        lose = []
        if not beats_seasonal:
            lose.append("seasonal-naive")
        if not beats_lastweek:
            lose.append("last-week carry-forward")
        lines.append(f"**FAIL — on pooled wMAPE the champion does NOT beat: "
                     f"{', '.join(lose)}.** Champion(recursive) "
                     f"{_fmt(pooled_stats['champion_recursive']['wmape'])} vs seasonal-naive "
                     f"{_fmt(pooled_stats['seasonal_naive']['wmape'])} and last-week "
                     f"{_fmt(pooled_stats['naive_lastweek']['wmape'])}; fold wins "
                     f"{wins}/{n_folds}. This is the honest read: for pure 4-week volume "
                     f"FORECASTING the simple benchmark is competitive. The champion's "
                     f"validated job is decision-making (isolating the discount effect), "
                     f"not beating naive forecasters — but a buyer should see this table.\n")
    lines.append("## Pooled metrics (all folds, identical cell-weeks per model)\n")
    lines.append("| model | n cell-weeks | MAPE | wMAPE | bias (MPE) | R2 (log) |")
    lines.append("|---|---|---|---|---|---|")
    for m in MODELS:
        s = pooled_stats[m]
        lines.append(f"| {m} | {s['n']} | {_fmt(s['mape'], pct=True)} | "
                     f"{_fmt(s['wmape'], pct=True)} | {_fmt(s['bias_mpe'], pct=True)} | "
                     f"{_fmt(s['r2_log'])} |")
    lines.append("\n## Per-fold table\n")
    lines.append("| fold | origin | test window | train wks | model | n | wMAPE | MAPE | bias | R2(log) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in folds_df[folds_df["fold"] != "POOLED"].iterrows():
        lines.append(f"| {r['fold']} | {r['origin_week']} | {r['test_start']}..{r['test_end']} | "
                     f"{r['train_weeks']} | {r['model']} | {r['n_cellweeks']} | "
                     f"{r['wmape']*100:.1f}% | {r['mape']*100:.1f}% | {r['bias_mpe']*100:+.1f}% | "
                     f"{r['r2_log']:.3f} |")
    lines.append("\n## How to read this\n")
    lines.append("- **wMAPE** (headline): rupee-volume-weighted error — big sellers count more. "
                 "Lower is better; the champion must beat BOTH benchmarks to claim forecasting skill.")
    lines.append("- **champion_recursive** is the honest 4-week-ahead number: the model only sees "
                 "data up to the origin and feeds its own predictions forward as lags.")
    lines.append("- **champion_1step** feeds realized last-week sales in as lags (exactly what the "
                 "production holdout_r2 measures). It is an upper bound, not a plan-horizon forecast.")
    lines.append("- **seasonal_naive**: same cell, same week-of-year last year when available — with "
                 "~6 months of history it almost always falls back to the last-4-week mean "
                 "(see n_true_seasonal_matches in backtest_folds.csv).")
    lines.append("- **naive_lastweek**: copy last week's units forward — the simplest legacy plan.")
    lines.append("- **bias** > 0 = over-forecasting volume; < 0 = under-forecasting.")
    lines.append("- Rows are scored only where every model could predict (cell needs >=2 training "
                 "weeks and a category model); the coverage column in the CSV shows the kept share.")
    if skipped:
        lines.append("\n## Skipped folds / notes\n")
        for s in skipped:
            lines.append(f"- {s}")
    lines.append("\n## Caveats (read before quoting)\n")
    lines.append("- The champion is a confounder-controlled DECISION model (isolates the discount "
                 "effect); this backtest grades it on volume forecasting, which is a harder, "
                 "different job. Losing to a naive forecaster here does not invalidate the "
                 "cut/reinvest logic — but it does mean the model should not be sold as a demand "
                 "forecaster (consistent with project validation notes).")
    lines.append("- Test-week discount/OSA/SOV values are REALIZED, not planned — all models get "
                 "the same information, so the comparison is fair, but absolute errors are "
                 "optimistic vs true ex-ante forecasting.")
    lines.append(f"- Folds with <{MIN_TRAIN_WEEKS} training weeks are skipped; with a short "
                 f"history the earliest folds train on thin data.")
    path = os.path.join(OUT_DIR, "BACKTEST_REPORT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[backtest] report -> {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rolling-origin backtest of the production champion.")
    ap.add_argument("--n-origins", type=int, default=5, help="number of walk-forward origins (>=4 for paper parity)")
    ap.add_argument("--step-weeks", type=int, default=2, help="spacing between origins, in weeks")
    ap.add_argument("--horizon-weeks", type=int, default=4, help="forward scoring window per origin")
    ap.add_argument("--max-minutes", type=float, default=8.0, help="wall-clock budget; later folds are skipped past this")
    a = ap.parse_args()
    run_backtest(a.n_origins, a.step_weeks, a.horizon_weeks, a.max_minutes)
