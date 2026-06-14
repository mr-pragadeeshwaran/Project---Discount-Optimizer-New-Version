"""
leakage.py — "real vs borrowed vs stolen" decomposition of promo uplift.

When a cell runs a deep discount, units spike. That spike is NOT all new
demand. We split it into three OBSERVATIONAL PROXIES (correlational, not a
controlled causal estimate — there is no counterfactual or confounder
adjustment, so read these as directional signals, not proven cause-and-effect):

  • REAL incremental   — uplift not associated with a later own-cell dip or a
    concurrent sibling dip; a proxy for genuinely new demand.
  • BORROWED (φ, pull-forward) — uplift OFFSET by a dip below baseline in the
    weeks AFTER the promo. Consistent with customers stocking up — but a
    post-promo dip can also be mean-reversion or just the return to the cell's
    normal (higher) price.
  • STOLEN (κ, cannibalization) — uplift ASSOCIATED WITH a same-window dip in
    the brand's own sibling packs (same real category + city). Not controlled
    for the sibling's own stockouts / price moves / seasonality, so treat κ as
    an upper-bound proxy.

Everything is UNIT-based — no COGS / margin assumptions. Output per cell:
  pull_forward (φ), cannibalization (κ), true_incremental_frac = clip(1−φ−κ,0,1),
  n_episodes, leakage_confidence. Confidence flags:
    no_promo            — no clear promo variation to measure
    always_promo        — chronically deep discount, can't form a clean baseline
    low/medium/high     — measured (episode count)
    *_no_siblings       — no sibling pack to measure cannibalization against
    *_over_attributed   — φ+κ exceeded 1 (decomposition unreliable — treat with care)
"""
import numpy as np
import pandas as pd
import v4_config as cfg

COL = cfg.COL
POST_WINDOW_CAP_DAYS = 14    # max CALENDAR days after a promo to look for the dip
MIN_EPISODE_UPLIFT_X = 1.0   # episode uplift must exceed this × baseline-day units
MIN_EPISODE_UPLIFT_ABS = 5.0 # …and at least this many absolute units (noise floor)
MIN_BASELINE_UNITS = 1.0     # cells thinner than this are not decomposable
CATCHALL_CATEGORIES = {"Other", "Unknown", ""}


def _cell_series(g):
    """(units, discount, dates) sorted by date for one cell's regular days."""
    g = g.sort_values(COL["date"])
    return (g[COL["offtake_qty"]].to_numpy(float),
            g["discount_pct"].to_numpy(float),
            pd.to_datetime(g[COL["date"]]).to_numpy())


def _episodes(promo_mask):
    """Maximal runs of True → list of (start, end_exclusive) array positions."""
    eps, i, n = [], 0, len(promo_mask)
    while i < n:
        if promo_mask[i]:
            j = i
            while j < n and promo_mask[j]:
                j += 1
            eps.append((i, j))
            i = j
        else:
            i += 1
    return eps


def _baseline_and_threshold(u, d):
    """
    Baseline daily units (normal-price days), the promo discount cut-off, and a
    regime tag: 'ok' | 'no_variation' | 'always_promo'.
    """
    if len(d) < 10:
        return (float(np.median(u)) if len(u) else 0.0), None, 0.0, "no_variation"
    p40 = np.percentile(d, 40)
    normal = u[d <= p40]
    baseline = float(np.median(normal)) if len(normal) else float(np.median(u))
    base_disc = float(np.median(d[d <= p40])) if (d <= p40).any() else float(np.median(d))
    high_thr = float(np.percentile(d, 75))
    if high_thr < base_disc + 3.0:
        # No within-cell promo spread. Distinguish a chronically-deep discounter
        # (can't form a clean baseline) from a genuinely flat low-discount cell.
        regime = "always_promo" if base_disc >= 15.0 else "no_variation"
        return baseline, None, base_disc, regime
    return baseline, high_thr, base_disc, "ok"


def decompose_leakage(feat_df):
    """
    Returns a per-cell DataFrame:
      cell_id, pull_forward, cannibalization, true_incremental_frac,
      n_episodes, total_uplift_units, leakage_confidence
    """
    df = feat_df.copy()
    if "is_regular_day" in df.columns:
        df = df[df["is_regular_day"] == 1]
    if "cell_id" not in df.columns:
        g = COL["grammage"]
        if g in df.columns:
            df["cell_id"] = (df[COL["product_id"]].astype(str) + "_"
                             + df[g].astype(str) + "_" + df[COL["city"]].astype(str))
        else:
            df["cell_id"] = df[COL["product_id"]].astype(str) + "_" + df[COL["city"]].astype(str)

    # Pre-index each cell's daily series
    cell_index = {}
    for cid, g in df.groupby("cell_id"):
        u, d, dates = _cell_series(g)
        base, thr, base_disc, regime = _baseline_and_threshold(u, d)
        cell_index[cid] = {
            "u": u, "d": d, "dates": dates, "baseline": base, "thr": thr,
            "regime": regime,
            "cat": g["category"].iloc[0] if "category" in g.columns else "",
            "city": g[COL["city"]].iloc[0],
        }

    # Sibling map: same REAL category + city, different cell. Catch-all buckets
    # ('Other'/'Unknown') are NOT real categories, so they get no siblings —
    # otherwise unrelated SKUs would fabricate cannibalization.
    sib_map = {}
    for cid, info in cell_index.items():
        if info["cat"] in CATCHALL_CATEGORIES:
            sib_map[cid] = []
            continue
        sib_map[cid] = [o for o, oi in cell_index.items()
                        if o != cid and oi["cat"] == info["cat"]
                        and oi["city"] == info["city"]
                        and oi["cat"] not in CATCHALL_CATEGORIES]

    rows = []
    for cid, info in cell_index.items():
        u, d, dates = info["u"], info["d"], info["dates"]
        base, thr, regime = info["baseline"], info["thr"], info["regime"]

        def _emit(conf):
            rows.append({"cell_id": cid, "pull_forward": 0.0, "cannibalization": 0.0,
                         "true_incremental_frac": 1.0, "n_episodes": 0,
                         "total_uplift_units": 0.0, "leakage_confidence": conf})

        if thr is None or base < MIN_BASELINE_UNITS or len(u) < 20:
            _emit(regime if regime in ("always_promo", "no_variation") else "no_promo")
            continue

        promo = d >= thr
        eps = _episodes(promo)
        tot_uplift = tot_postdef = tot_sibdef = 0.0
        n_used = 0
        for k, (i, j) in enumerate(eps):
            uplift = float(np.sum(np.maximum(u[i:j] - base, 0.0)))
            if uplift < max(MIN_EPISODE_UPLIFT_X * base, MIN_EPISODE_UPLIFT_ABS):
                continue
            n_used += 1
            tot_uplift += uplift

            # pull-forward: dip below baseline in a CALENDAR window after the
            # promo, capped at POST_WINDOW_CAP_DAYS and never bleeding into the
            # next episode (avoids double-counting a shared dip).
            ep_end = dates[j - 1]
            ep_len_days = int((pd.Timestamp(ep_end) - pd.Timestamp(dates[i])).days) + 1
            w_days = min(2 * ep_len_days, POST_WINDOW_CAP_DAYS)
            post_end = np.datetime64(pd.Timestamp(ep_end) + pd.Timedelta(days=w_days))
            post_mask = (dates > ep_end) & (dates <= post_end)
            if k + 1 < len(eps):                      # stop before the next promo
                post_mask &= dates < dates[eps[k + 1][0]]
            tot_postdef += float(np.sum(np.maximum(base - u[post_mask], 0.0)))

            # cannibalization: sibling dip over the SAME calendar days
            ep_start = dates[i]
            for sib in sib_map[cid]:
                si = cell_index[sib]
                if si["baseline"] < MIN_BASELINE_UNITS:
                    continue
                m = (si["dates"] >= ep_start) & (si["dates"] <= ep_end)
                if m.any():
                    tot_sibdef += float(np.sum(np.maximum(si["baseline"] - si["u"][m], 0.0)))

        if tot_uplift <= 0 or n_used == 0:
            _emit("no_promo")
            continue

        raw_phi = tot_postdef / tot_uplift
        has_sib = len(sib_map[cid]) > 0
        raw_kappa = (tot_sibdef / tot_uplift) if has_sib else 0.0
        phi = float(np.clip(raw_phi, 0.0, 1.0))
        kappa = float(np.clip(raw_kappa, 0.0, 1.0))
        true_frac = float(np.clip(1.0 - phi - kappa, 0.0, 1.0))

        conf = ("high" if n_used >= 3 else "medium" if n_used >= 2 else "low")
        if not has_sib:
            conf += "_no_siblings"
        if (raw_phi + raw_kappa) > 1.05:   # decomposition over-attributed → unreliable
            conf += "_over_attributed"
        rows.append({
            "cell_id": cid,
            "pull_forward": round(phi, 3),
            "cannibalization": round(kappa, 3),
            "true_incremental_frac": round(true_frac, 3),
            "n_episodes": n_used,
            "total_uplift_units": round(tot_uplift, 0),
            "leakage_confidence": conf,
        })

    return pd.DataFrame(rows)


# cells whose leakage was NOT actually measured (for honest summary counts)
NOT_MEASURED = {"no_promo", "always_promo", "no_variation", "n/a", "unavailable"}


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
    from stage2_preparation.prepare import prepare_fact_table
    from stage3_features.features import engineer_features
    raw = ingest_all_sales(); cal = load_event_calendar()
    feat = engineer_features(prepare_fact_table(raw, cal))
    lk = decompose_leakage(feat)
    print("\n" + "=" * 72)
    print("  LEAKAGE DECOMPOSITION (per cell)")
    print("=" * 72)
    with pd.option_context("display.max_rows", 50, "display.width", 120):
        print(lk.sort_values("pull_forward", ascending=False).to_string(index=False))
    measured = lk[~lk["leakage_confidence"].isin(NOT_MEASURED)]
    if not measured.empty:
        print(f"\n  Cells with measured promo leakage: {len(measured)}/{len(lk)}")
        print(f"  Median pull-forward φ:   {measured['pull_forward'].median():.2f}")
        print(f"  Median cannibalization κ:{measured['cannibalization'].median():.2f}")
        print(f"  Median true-incremental: {measured['true_incremental_frac'].median():.2f}")
