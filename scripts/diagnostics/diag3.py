"""
Investigate why Test MAPE=582% and R2=-0.33.
Checks: test vs train distribution, discount patterns, volume spikes.
"""
import pandas as pd
import numpy as np
import glob

# Load all data
frames = []
for f in glob.glob(r'input_data/*.xlsx'):
    df = pd.read_excel(f)
    frames.append(df)
df = pd.concat(frames, ignore_index=True)

# Coerce
for c in ['MRP','PRICE','OFFTAKE_QTY','WT_DISCOUNT_PCT','WT_AVAILABILITY_PCT']:
    df[c] = pd.to_numeric(df[c], errors='coerce')
df['DATE'] = pd.to_datetime(df['DATE'])
df['GRAMMAGE'] = df['GRAMMAGE'].astype(str).str.strip()

# Own brand only
own = df[df['BRAND'].str.contains('24 Mantra', case=False, na=False)].copy()

# The train/test split used in Stage 4:
# Regular days = not event & not OOS (availability >= 50%)
own['is_oos'] = (own['WT_AVAILABILITY_PCT'] < 50).astype(int)
regular = own[own['is_oos'] == 0].copy()

dates_sorted = sorted(regular['DATE'].unique())
split_idx = int(len(dates_sorted) * 0.80)
split_date = dates_sorted[split_idx]
print(f"Train/test split date: {split_date.date()}")
print(f"Train dates: {dates_sorted[0].date()} to {split_date.date()}")
print(f"Test dates:  {dates_sorted[split_idx+1].date()} to {dates_sorted[-1].date()}")

train = regular[regular['DATE'] <= split_date]
test  = regular[regular['DATE'] > split_date]

print(f"\nTrain rows: {len(train):,}  |  Test rows: {len(test):,}")

# Compare distributions
print("\n--- DISCOUNT DISTRIBUTION (WT_DISCOUNT_PCT) ---")
print(f"Train: mean={train['WT_DISCOUNT_PCT'].mean():.1f}%  std={train['WT_DISCOUNT_PCT'].std():.1f}%  "
      f"min={train['WT_DISCOUNT_PCT'].min():.1f}%  max={train['WT_DISCOUNT_PCT'].max():.1f}%")
print(f"Test:  mean={test['WT_DISCOUNT_PCT'].mean():.1f}%  std={test['WT_DISCOUNT_PCT'].std():.1f}%  "
      f"min={test['WT_DISCOUNT_PCT'].min():.1f}%  max={test['WT_DISCOUNT_PCT'].max():.1f}%")

print("\n--- UNITS DISTRIBUTION (OFFTAKE_QTY) ---")
print(f"Train: mean={train['OFFTAKE_QTY'].mean():.1f}  std={train['OFFTAKE_QTY'].std():.1f}  "
      f"min={train['OFFTAKE_QTY'].min():.1f}  max={train['OFFTAKE_QTY'].max():.1f}  "
      f"median={train['OFFTAKE_QTY'].median():.1f}")
print(f"Test:  mean={test['OFFTAKE_QTY'].mean():.1f}  std={test['OFFTAKE_QTY'].std():.1f}  "
      f"min={test['OFFTAKE_QTY'].min():.1f}  max={test['OFFTAKE_QTY'].max():.1f}  "
      f"median={test['OFFTAKE_QTY'].median():.1f}")

print("\n--- log(UNITS) DISTRIBUTION ---")
train_log = np.log(train['OFFTAKE_QTY'].clip(lower=0.1))
test_log  = np.log(test['OFFTAKE_QTY'].clip(lower=0.1))
print(f"Train: mean={train_log.mean():.3f}  std={train_log.std():.3f}")
print(f"Test:  mean={test_log.mean():.3f}  std={test_log.std():.3f}")

# Daily averages by period
print("\n--- MONTHLY AVERAGES (test period breakdown) ---")
own['month_year'] = own['DATE'].dt.to_period('M')
monthly = own.groupby('month_year').agg(
    avg_units=('OFFTAKE_QTY','mean'),
    avg_discount=('WT_DISCOUNT_PCT','mean'),
    n_rows=('OFFTAKE_QTY','count')
).reset_index()
print(monthly.to_string(index=False))

# Check if test period has unusual volume spikes
print("\n--- TOP 20 HIGHEST UNIT DAYS IN TEST PERIOD ---")
test_top = test.nlargest(20, 'OFFTAKE_QTY')[['DATE','GRAMMAGE','GC_CITY','OFFTAKE_QTY','WT_DISCOUNT_PCT','TITLE']]
print(test_top.to_string(index=False))

# Check log-space MAPE (which is what the model actually minimises)
print("\n--- LOG-SPACE METRICS ---")
print("If model predicts log(units) with MAPE=X%, that means raw units MAPE can explode")
print("because exp() amplifies errors non-linearly.")
print(f"Train log(units) mean={train_log.mean():.3f}, std={train_log.std():.3f}")
print(f"Test  log(units) mean={test_log.mean():.3f}, std={test_log.std():.3f}")
print(f"Ratio of means (train/test): {train_log.mean()/test_log.mean():.3f}")
