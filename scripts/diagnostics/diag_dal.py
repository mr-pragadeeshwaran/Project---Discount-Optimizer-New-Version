"""Investigate why Moong Dal 500g cells collapse in the test period."""
import os, sys, glob
import pandas as pd
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import v4_config as cfg
COLS = cfg.COL

runs = sorted(glob.glob(os.path.join(cfg.OUTPUT_DIR, "*", "features.csv")))
df = pd.read_csv(runs[-1], parse_dates=[COLS["date"]])
reg = df[df["is_regular_day"] == 1].copy()

# Focus on Moong Dal 500g (product_id 126995)
dal = reg[reg[COLS["product_id"]] == 126995].copy()
print(f"Moong Dal 500g rows: {len(dal):,} | regular days only")
print(f"Date range: {dal[COLS['date']].min().date()} to {dal[COLS['date']].max().date()}")

# Pick the split date (matches Stage 4)
dates = sorted(reg[COLS["date"]].unique())
sd = pd.Timestamp(dates[int(len(dates) * 0.8)])
print(f"Train/test split date: {sd.date()}")

# Per-period stats per city
print("\nPer-city: TRAIN avg | TEST avg | change")
print(f"{'city':22s} {'metric':18s} {'train':>10s} {'test':>10s} {'delta':>10s}")
for city in sorted(dal[COLS["city"]].unique()):
    sub = dal[dal[COLS["city"]] == city]
    tr = sub[sub[COLS["date"]] <= sd]; te = sub[sub[COLS["date"]] > sd]
    if len(tr) < 5 or len(te) < 5: continue
    for metric in ["selling_price", "discount_pct", COLS["offtake_qty"],
                   "osa_rolling_7d", COLS["competitor_price"]]:
        if metric not in sub.columns: continue
        tv = tr[metric].mean(); ev = te[metric].mean()
        print(f"{city:22s} {metric:18s} {tv:10.2f} {ev:10.2f} {ev-tv:+10.2f}")
    print()

# Did units crater across the board?
print("\nMoong Dal 500g — daily units mean per month")
dal["yyyymm"] = dal[COLS["date"]].dt.strftime("%Y-%m")
print(dal.groupby(["yyyymm"])[COLS["offtake_qty"]].agg(["mean", "median", "count"]).round(1))

# Discount distribution per period
print("\nDiscount distribution (Dal 500g, train vs test)")
tr = dal[dal[COLS["date"]] <= sd]; te = dal[dal[COLS["date"]] > sd]
print("  TRAIN discount: ", tr["discount_pct"].describe().round(1).to_dict())
print("  TEST discount:  ", te["discount_pct"].describe().round(1).to_dict())

# Was there a price-level shock?
print("\nDal 500g selling_price by week (last 8 weeks of train + first 8 of test)")
dal["week"] = dal[COLS["date"]].dt.to_period("W").astype(str)
weekly = dal.groupby("week").agg(
    price=("selling_price", "mean"),
    units=(COLS["offtake_qty"], "mean"),
    disc=("discount_pct", "mean"),
    n=(COLS["date"], "count"),
).round(2)
# Show around split
all_weeks = sorted(weekly.index.unique())
split_week = pd.Timestamp(sd).to_period("W").strftime("%Y-%m-%d/%Y-%m-%d")
print(weekly.tail(20))

# Is there an OOS pattern we missed?
print("\nFraction of low-availability days (< 60%) per month")
low_oos = dal[dal["osa_rolling_7d"] < 0.6]
print(low_oos.groupby("yyyymm")[COLS["date"]].count())
