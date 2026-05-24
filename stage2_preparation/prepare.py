"""
Stage 2 — Data Preparation.

Cleans and unifies raw data into a single fact table at SKU × Grammage × City × Day grain.
Flags event days, festivals, OOS days, and marks regular training days.

KEY DESIGN:
  - stable_mrp   : 90th percentile MRP per SKU-Grammage — the clean "label price"
  - selling_price: stable_mrp × (1 − discount/100) — what the consumer actually pays
  - discount_pct : WT_DISCOUNT_PCT — the brand's lever (0-52% variation)

MODEL PHILOSOPHY (dual-signal):
  log(units) ~ log(selling_price)   ← consumer sees THIS price → primary signal
             + discount_pct         ← the red "X% OFF" badge → psychological trigger
  This separates PRICE LEVEL effect from DEAL SIGNAL effect.
"""
import os
import pandas as pd
import numpy as np
import v4_config as cfg


def prepare_fact_table(raw_df: pd.DataFrame, calendar_df: pd.DataFrame,
                        run_dir: str = None) -> pd.DataFrame:
    """
    Clean, validate, and flag the raw combined data.

    Returns a fact table at SKU × Grammage × City × Day grain with flags for
    downstream filtering (event/OOS/regular days) and a stable MRP column.
    """
    C = cfg.COL
    df = raw_df.copy()
    print(f"  [Stage 2] Input: {len(df):,} rows")

    # Cell grouping keys — always include grammage if present so that
    # 500g and 1kg variants of the same product are kept fully separate.
    has_grammage = C["grammage"] in df.columns
    grp = [C["product_id"], C["city"]]
    if has_grammage:
        grp = [C["product_id"], C["grammage"], C["city"]]

    # ── 1. Fill missing values ──────────────────────────────────────
    # Forward-fill availability and price within each cell
    sort_keys = grp + [C["date"]]
    df = df.sort_values(sort_keys)

    for col in [C["availability"], C["price"], C["competitor_price"]]:
        if col in df.columns:
            df[col] = df.groupby(grp)[col].transform(
                lambda s: s.ffill().bfill()
            )

    # Fill remaining NaN in numeric columns with 0
    fill_zero = [C["offtake_mrp"], C["offtake_qty"], C["ad_sov"], C["discount_pct"]]
    for col in fill_zero:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    # ── 2. Compute STABLE MRP per SKU-Grammage ──────────────────────
    # The raw MRP column is a daily weighted average that drifts per city.
    # We compute a stable reference MRP (90th percentile of the raw MRP)
    # per (product_id, grammage) — this is the "label price" on the pack.
    mrp_grp = [C["product_id"]]
    if has_grammage:
        mrp_grp = [C["product_id"], C["grammage"]]

    df[C["mrp"]] = pd.to_numeric(df[C["mrp"]], errors="coerce")
    stable_mrp_map = df.groupby(mrp_grp)[C["mrp"]].quantile(0.90)
    df["stable_mrp"] = df.set_index(mrp_grp).index.map(stable_mrp_map)
    df["stable_mrp"] = df["stable_mrp"].fillna(df[C["mrp"]])

    print(f"  [Stage 2] Stable MRP (90th pct per SKU-grammage):")
    stable_summary = df.groupby(mrp_grp)["stable_mrp"].first()
    for idx, val in stable_summary.items():
        label = idx if isinstance(idx, str) else " | ".join(str(i) for i in idx)
        print(f"    {label}: ₹{val:.2f}")

    # ── 3. Use WT_DISCOUNT_PCT as the authoritative discount ─────────
    # WT_DISCOUNT_PCT is a weighted average discount across all transactions
    # for a city-day, which has genuine variation (0% to 52%).
    wt_disc_col = C["discount_pct"]  # "WT_DISCOUNT_PCT"
    if wt_disc_col in df.columns:
        df["discount_pct_actual"] = pd.to_numeric(df[wt_disc_col], errors="coerce").clip(lower=0, upper=80)
    else:
        # Fallback: derive from stable_mrp and price
        df["discount_pct_actual"] = ((df["stable_mrp"] - df[C["price"]]) / df["stable_mrp"] * 100).clip(0, 80)

    # ── 4. Compute SELLING PRICE — the consumer-facing price ─────────
    # selling_price = stable_mrp × (1 − discount/100)
    # This is the definitive price the consumer sees on Blinkit.
    # Using stable_mrp (not raw daily MRP) ensures this is clean and consistent.
    df["selling_price"] = (df["stable_mrp"] * (1 - df["discount_pct_actual"] / 100)).round(2)
    df["selling_price"] = df["selling_price"].clip(lower=1.0)  # floor at ₹1

    # ── 5. Validate price ranges ────────────────────────────────────

    # Availability: clamp to [0, 100]
    if C["availability"] in df.columns:
        df[C["availability"]] = df[C["availability"]].clip(0, 100)

    # Units: floor at 0
    if C["offtake_qty"] in df.columns:
        df[C["offtake_qty"]] = df[C["offtake_qty"]].clip(lower=0)

    # ── 5. Flag days ────────────────────────────────────────────────
    # OOS flag
    df["is_oos_day"] = (df[C["availability"]] < cfg.OSA_OOS_THRESHOLD).astype(int)

    # Event/festival flags from calendar
    df["is_event_day"] = 0
    df["is_festival"] = 0
    df["event_name"] = ""

    if not calendar_df.empty:
        cal = calendar_df.copy()
        cal["date"] = pd.to_datetime(cal["date"])

        festival_dates = set(cal[cal["event_type"] == "festival"]["date"].dt.date)
        platform_dates = set(cal[cal["event_type"] == "platform_sale"]["date"].dt.date)
        all_event_dates = festival_dates | platform_dates

        df_dates = df[C["date"]].dt.date
        df["is_festival"] = df_dates.isin(festival_dates).astype(int)
        df["is_event_day"] = df_dates.isin(all_event_dates).astype(int)

        event_lookup = cal.drop_duplicates(subset=["date"]).set_index("date")["event_name"]
        df["event_name"] = df[C["date"]].map(event_lookup).fillna("")

    # Regular day: NOT event AND NOT OOS (used for model training)
    df["is_regular_day"] = ((df["is_event_day"] == 0) & (df["is_oos_day"] == 0)).astype(int)

    # ── 5b. Per-cell outlier detection (product × grammage × city) ──
    # On regular days only, flag rows whose log(units) is > Z_THRESHOLD
    # sigma from the cell's own mean. Save audit trail to outliers_removed.csv.
    df["is_outlier"] = 0
    df["outlier_reason"] = ""
    outlier_records = []

    # Compute z-score on regular-day rows only, per cell group
    reg_mask = df["is_regular_day"] == 1
    qty_col  = C["offtake_qty"]
    # log units (floor at 0.1 to handle zeros)
    log_q    = np.log(df[qty_col].clip(lower=0.1))

    for key, gdf in df[reg_mask].groupby(grp):
        if len(gdf) < cfg.OUTLIER_MIN_OBS_PER_CELL:
            continue
        log_vals = np.log(gdf[qty_col].clip(lower=0.1).values)
        mu  = float(np.mean(log_vals))
        sig = float(np.std(log_vals))
        if sig < 1e-6:
            continue
        z = (log_vals - mu) / sig
        out_mask = np.abs(z) > cfg.OUTLIER_Z_THRESHOLD
        if not out_mask.any():
            continue
        # Mark in main df
        out_idx = gdf.index[out_mask]
        df.loc[out_idx, "is_outlier"] = 1
        # Build label
        if has_grammage:
            pid, grm, city = key
            cell_label = f"{pid}_{grm}_{city}"
        else:
            pid, city = key
            grm = None
            cell_label = f"{pid}_{city}"
        # Record each outlier with context for audit
        for i, idx in zip(np.where(out_mask)[0], out_idx):
            row = df.loc[idx]
            z_val = float(z[i])
            direction = "HIGH" if z_val > 0 else "LOW"
            outlier_records.append({
                "cell_id":          cell_label,
                "product_id":       pid,
                "grammage":         grm,
                "city":             city,
                "date":             pd.to_datetime(row[C["date"]]).date(),
                "offtake_qty":      float(row[qty_col]),
                "cell_mean_units":  round(float(np.exp(mu)), 1),
                "z_score":          round(z_val, 2),
                "direction":        direction,
                "discount_pct":     round(float(row.get("discount_pct_actual", 0)), 1),
                "availability_pct": round(float(row.get(C["availability"], 0)), 1),
                "reason":           f"|z|={abs(z_val):.2f} > {cfg.OUTLIER_Z_THRESHOLD} ({direction} spike)",
            })

    # Outliers must NOT be used for training — downgrade is_regular_day
    df.loc[df["is_outlier"] == 1, "is_regular_day"] = 0
    df.loc[df["is_outlier"] == 1, "outlier_reason"] = "Statistical outlier (|z|>threshold)"

    # Per-product audit summary
    n_outliers = int(df["is_outlier"].sum())
    if n_outliers and outlier_records:
        out_df = pd.DataFrame(outlier_records).sort_values(["product_id", "city", "date"])
        # Per-product counts
        prod_grp_col = C["product_id"] if not has_grammage else None
        print(f"  [Stage 2] Outlier detection (per cell, |z|>{cfg.OUTLIER_Z_THRESHOLD}):")
        if has_grammage:
            for (pid, grm), gg in out_df.groupby(["product_id", "grammage"]):
                print(f"    Product {pid} | {grm}: {len(gg)} outliers removed "
                      f"({(gg['direction']=='HIGH').sum()} spikes, "
                      f"{(gg['direction']=='LOW').sum()} dips)")
        else:
            for pid, gg in out_df.groupby("product_id"):
                print(f"    Product {pid}: {len(gg)} outliers removed "
                      f"({(gg['direction']=='HIGH').sum()} spikes, "
                      f"{(gg['direction']=='LOW').sum()} dips)")
        print(f"    Total outliers excluded from training: {n_outliers}")
        if run_dir:
            out_path = os.path.join(run_dir, "outliers_removed.csv")
            out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
            print(f"    Audit trail: {out_path}")
    else:
        print(f"  [Stage 2] Outlier detection: 0 outliers found "
              f"(|z|>{cfg.OUTLIER_Z_THRESHOLD} threshold)")

    # ── 6. Create cell identifier ───────────────────────────────────
    # Include grammage so '500g_Delhi' and '1kg_Delhi' are different cells.
    if has_grammage:
        df["cell_id"] = (
            df[C["product_id"]].astype(str) + "_"
            + df[C["grammage"]].astype(str) + "_"
            + df[C["city"]].astype(str)
        )
    else:
        df["cell_id"] = df[C["product_id"]].astype(str) + "_" + df[C["city"]].astype(str)

    # ── 7. Summary stats ────────────────────────────────────────────
    n_cells = df["cell_id"].nunique()
    n_regular = df["is_regular_day"].sum()
    n_event = df["is_event_day"].sum()
    n_oos = df["is_oos_day"].sum()

    if has_grammage:
        gram_counts = df.groupby(C["grammage"])["cell_id"].nunique()
        gram_summary = ", ".join(f"{g}: {c} cells" for g, c in gram_counts.items())
        print(f"  [Stage 2] Fact table: {len(df):,} rows | {n_cells} cells ({gram_summary})")
    else:
        print(f"  [Stage 2] Fact table: {len(df):,} rows | {n_cells} cells")

    print(f"    Regular days: {n_regular:,} | Event days: {n_event:,} | OOS days: {n_oos:,}")
    print(f"    Training-eligible (regular): {n_regular/len(df)*100:.1f}%")
    print(f"    Discount range on regular days: "
          f"{df.loc[df['is_regular_day']==1,'discount_pct_actual'].min():.1f}% – "
          f"{df.loc[df['is_regular_day']==1,'discount_pct_actual'].max():.1f}%  "
          f"(mean={df.loc[df['is_regular_day']==1,'discount_pct_actual'].mean():.1f}%)")

    return df.reset_index(drop=True)


if __name__ == "__main__":
    from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar
    raw = ingest_all_sales()
    cal = load_event_calendar()
    fact = prepare_fact_table(raw, cal)
    print(f"\nFact table shape: {fact.shape}")
    print(f"Columns: {list(fact.columns)}")
