"""
validate.py — fail-loud input checks (the done-for-you operator's safety net).

When you load a NEW client's aligned data, this catches the common ways it can
be wrong BEFORE the pipeline runs on it — so you get a clear, actionable error
instead of a cryptic crash deep in Stage 4 or (worse) silently wrong numbers.

Two gates:
  validate_columns(df)  — HARD fail if a required column is missing.
  validate_quality(df)  — HARD fail on fatal data problems (no rows, no dates,
                          <2 cells); WARN on soft issues (negatives, out-of-range
                          discounts, mostly-missing fields) so you can decide.

validate_quality also runs three named data-quality checks (PepsiCo-paper §2.1.1
input hygiene, all WARN-level — they never block the pipeline):
  1. unexplained-spike check       — high-demand days with NO recorded deep promo
                                     and NO festival nearby (suspect data or an
                                     unlogged promo; the prepare stage will drop
                                     the extreme ones as outliers).
  2. margin/price-consistency check — selling price above MRP, below the COGS
                                     proxy, PRICE vs discount-pct disagreement,
                                     and sales recorded while availability was 0.
  3. SKU-identity-continuity check — product_ids that vanish while new ids appear
                                     (identifier churn silently fragments a cell's
                                     history across two ids).
"""
import numpy as np
import pandas as pd
import v4_config as cfg

# Columns the pipeline cannot run without (mapped via cfg.COL)
HARD_REQUIRED = ["product_id", "city", "date", "offtake_qty", "mrp",
                 "discount_pct", "title"]
# Used when present, defaulted/optional when absent
SOFT_OPTIONAL = ["grammage", "availability", "ad_sov", "competitor_price",
                 "price", "offtake_mrp", "brand"]


def validate_columns(df: pd.DataFrame) -> None:
    """Raise with an actionable message if any required column is absent."""
    C = cfg.COL
    missing = [C[k] for k in HARD_REQUIRED if C[k] not in df.columns]
    if missing:
        present = list(map(str, df.columns))
        raise ValueError(
            "Input data is missing required column(s): " + ", ".join(missing) +
            ".\n  Align the client's export to the expected names (see v4_config.COL). "
            "Columns found in the file: " + ", ".join(present[:40]) +
            (" …" if len(present) > 40 else ""))
    soft_missing = [C[k] for k in SOFT_OPTIONAL if C[k] not in df.columns]
    if soft_missing:
        print(f"  [validate] note: optional columns absent (defaults will be used): {soft_missing}")


def validate_quality(df: pd.DataFrame) -> list:
    """
    Check basic data quality. HARD-fails on fatal problems; returns a list of
    soft warnings (also printed). Run AFTER the own-brand filter.
    """
    C = cfg.COL
    n = len(df)
    if n == 0:
        raise ValueError("No rows to model after ingestion/brand filter — "
                         "check BRAND_NAME and that the file isn't empty.")
    if pd.to_datetime(df[C["date"]], errors="coerce").isna().all():
        raise ValueError(f"Column '{C['date']}' has no parseable dates.")

    grp = [C["product_id"], C["city"]]
    if C["grammage"] in df.columns:
        grp = [C["product_id"], C["grammage"], C["city"]]
    n_cells = df.groupby(grp).ngroups
    if n_cells < 2:
        raise ValueError(f"Only {n_cells} cell (SKU×city) after filtering — need ≥2 to model. "
                         "Check the brand filter and that the data spans multiple cities/SKUs.")

    issues = []
    q = pd.to_numeric(df[C["offtake_qty"]], errors="coerce")
    if int((q < 0).sum()):
        issues.append(f"{int((q < 0).sum())} rows have negative units")
    if q.isna().mean() > 0.5:
        issues.append("over half of OFFTAKE_QTY is non-numeric/NaN")
    mrp = pd.to_numeric(df[C["mrp"]], errors="coerce")
    if (mrp.isna() | (mrp <= 0)).mean() > 0.5:
        issues.append("MRP is mostly missing or ≤ 0")
    d = pd.to_numeric(df[C["discount_pct"]], errors="coerce")
    oor = int(((d < 0) | (d > 100)).sum())
    if oor:
        issues.append(f"{oor} rows have discount outside 0–100%")
    if C["availability"] in df.columns:
        a = pd.to_numeric(df[C["availability"]], errors="coerce")
        if int(((a < 0) | (a > 100)).sum()):
            issues.append("some availability values are outside 0–100")

    # ── Named checks (paper §2.1.1) — WARN-level, never raise. ──────────────
    issues += _check_unexplained_spikes(df)
    issues += _check_margin_price_consistency(df)
    issues += _check_sku_identity_continuity(df)

    if issues:
        print("  [validate] ⚠ data-quality notes (pipeline will still run):")
        for it in issues:
            print(f"     - {it}")
    else:
        print(f"  [validate] ✓ {n:,} rows · {n_cells} cells · schema & basic quality OK")
    return issues


# ---------------------------------------------------------------------------
# Named WARN-level checks. Each returns a list of issue strings (possibly []).
# All are defensive: a check that cannot run (missing column, unparseable data)
# quietly returns [] — validation must never crash the pipeline it protects.
# ---------------------------------------------------------------------------

def _festival_days():
    """Set of dates flagged by the event calendar (festival ± window + platform events)."""
    days = set()
    win = int(getattr(cfg, "FESTIVAL_WINDOW_DAYS", 2))
    for d in getattr(cfg, "FESTIVAL_DATES", {}):
        try:
            t = pd.Timestamp(d)
        except Exception:
            continue
        for k in range(-win, win + 1):
            days.add((t + pd.Timedelta(days=k)).normalize())
    for (s, e) in getattr(cfg, "PLATFORM_EVENT_WINDOWS", {}):
        try:
            for t in pd.date_range(s, e):
                days.add(t.normalize())
        except Exception:
            continue
    return days


def _cell_key(df):
    """product×grammage×city key, matching the cell grain used elsewhere."""
    C = cfg.COL
    key = df[C["product_id"]].astype(str)
    if C["grammage"] in df.columns:
        key = key + "|" + df[C["grammage"]].astype(str)
    return key + "|" + df[C["city"]].astype(str)


def _check_unexplained_spikes(df) -> list:
    """Check 1 — unexplained-spike: demand spike days (per-cell log-units z-score >
    OUTLIER_Z_THRESHOLD) with neither a deep discount (>= max(20%, the cell's own
    p75 discount)) nor a festival/platform event nearby. A spike WITH a promo is
    explained; a spike WITHOUT one is suspect (data error or unlogged promo)."""
    C = cfg.COL
    try:
        q = pd.to_numeric(df[C["offtake_qty"]], errors="coerce")
        d = pd.to_numeric(df[C["discount_pct"]], errors="coerce").fillna(0.0)
        dates = pd.to_datetime(df[C["date"]], errors="coerce").dt.normalize()
        key = _cell_key(df)
        pos = (q > 0) & dates.notna()
        if pos.sum() < 30:
            return []
        ln = pd.Series(np.log(q[pos].astype(float)), index=df.index[pos])
        g = ln.groupby(key[pos])
        mu, sd, cnt = g.transform("mean"), g.transform("std"), g.transform("count")
        min_obs = int(getattr(cfg, "OUTLIER_MIN_OBS_PER_CELL", 30))
        z_thr = float(getattr(cfg, "OUTLIER_Z_THRESHOLD", 2.0))
        ok = (cnt >= min_obs) & (sd > 1e-9)
        spike_idx = ln.index[ok & ((ln - mu) / sd.where(sd > 1e-9) > z_thr)]
        if len(spike_idx) == 0:
            return []
        # Explanation A: deep discount that day (>= max(20, cell's p75 discount)).
        p75 = d.groupby(key).transform(lambda s: s.quantile(0.75))
        deep = d.loc[spike_idx] >= np.maximum(20.0, p75.loc[spike_idx])
        # Explanation B: festival / platform-event day (calendar ± window).
        fest = dates.loc[spike_idx].isin(_festival_days())
        n_unexplained = int((~deep & ~fest).sum())
        if n_unexplained:
            return [f"unexplained-spike check: {n_unexplained} of {len(spike_idx)} demand-spike "
                    f"days (per-cell z>{z_thr:g}) have NO deep promo and NO festival nearby — "
                    f"suspect rows (data error or unlogged promo); extreme ones will be dropped "
                    f"as outliers in Stage 2"]
        return []
    except Exception as e:               # never let a WARN check kill validation
        print(f"  [validate] note: unexplained-spike check skipped ({e})")
        return []


def _check_margin_price_consistency(df) -> list:
    """Check 2 — margin/price-consistency: prices above MRP, selling price below the
    COGS proxy (implausible margin — data error or unrecorded funding), PRICE vs
    discount-pct disagreement, and units sold while availability was 0."""
    C = cfg.COL
    out = []
    try:
        n = max(len(df), 1)
        mrp = pd.to_numeric(df[C["mrp"]], errors="coerce")
        d = pd.to_numeric(df[C["discount_pct"]], errors="coerce")
        price = pd.to_numeric(df[C["price"]], errors="coerce") if C["price"] in df.columns \
            else pd.Series(np.nan, index=df.index)
        selling = price.where(price.notna() & (price > 0), mrp * (1 - d.fillna(0) / 100.0))
        valid = mrp.notna() & (mrp > 0)

        n_above = int((valid & price.notna() & (price > mrp * 1.02)).sum())
        if n_above:
            out.append(f"margin/price-consistency check: {n_above} rows ({n_above/n:.1%}) have "
                       f"PRICE above MRP (+2% tolerance) — impossible on-shelf price, check the export")
        cogs = float(getattr(cfg, "DEFAULT_COGS_PCT", 0.50))
        n_below = int((valid & selling.notna() & (selling < mrp * cogs)).sum())
        if n_below:
            out.append(f"margin/price-consistency check: {n_below} rows ({n_below/n:.1%}) sell below "
                       f"the COGS proxy ({cogs:.0%} of MRP) — implausible margin; data error or "
                       f"unrecorded platform funding")
        has_p = valid & price.notna() & (price > 0) & d.notna()
        if has_p.any():
            implied = (1 - price[has_p] / mrp[has_p]) * 100.0
            n_mis = int(((implied - d[has_p]).abs() > 5.0).sum())
            if n_mis:
                out.append(f"margin/price-consistency check: {n_mis} rows ({n_mis/n:.1%}) have PRICE "
                           f"and WT_DISCOUNT_PCT disagreeing by >5 ppt — two sources of truth conflict")
        if C["availability"] in df.columns:
            a = pd.to_numeric(df[C["availability"]], errors="coerce")
            q = pd.to_numeric(df[C["offtake_qty"]], errors="coerce")
            n_ghost = int(((q > 0) & (a == 0)).sum())
            if n_ghost:
                out.append(f"margin/price-consistency check: {n_ghost} rows sold units while "
                           f"availability was 0 — phantom sales or a lagging availability feed")
        return out
    except Exception as e:
        print(f"  [validate] note: margin/price-consistency check skipped ({e})")
        return out


def _check_sku_identity_continuity(df) -> list:
    """Check 3 — SKU-identity-continuity: product_ids that stop appearing while new
    ids start appearing (identifier churn). A relisted SKU under a new id splits one
    cell's history into two thin ones — elasticities and floors silently degrade."""
    C = cfg.COL
    try:
        dates = pd.to_datetime(df[C["date"]], errors="coerce")
        ok = dates.notna()
        if not ok.any():
            return []
        pid = df.loc[ok, C["product_id"]].astype(str)
        seen = pd.DataFrame({"pid": pid, "date": dates[ok]}).groupby("pid")["date"].agg(["min", "max"])
        max_date = dates.max()
        span_ok = (max_date - seen["min"].min()).days > 56   # need real history to judge churn
        if not span_ok:
            return []
        cutoff = max_date - pd.Timedelta(days=28)
        vanished = seen[(seen["max"] < cutoff)]
        newcomers = seen[(seen["min"] > cutoff)]
        if len(vanished) and len(newcomers):
            return [f"SKU-identity-continuity check: {len(vanished)} product_id(s) not seen in the "
                    f"last 28 days while {len(newcomers)} new id(s) appeared — possible identifier "
                    f"changes; the same SKU under a new id fragments its cell history "
                    f"(vanished e.g. {list(vanished.index[:3])}, new e.g. {list(newcomers.index[:3])})"]
        return []
    except Exception as e:
        print(f"  [validate] note: SKU-identity-continuity check skipped ({e})")
        return []
