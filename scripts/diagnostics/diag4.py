"""
Quick diagnostic: inspect random_effects structure from the fitted MixedLM
with per-cell random slopes to find the correct key names.
"""
import warnings
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import glob

# Load data
frames = []
for f in glob.glob(r'input_data/*.xlsx'):
    frames.append(pd.read_excel(f))
df = pd.concat(frames, ignore_index=True)

for c in ['MRP','PRICE','OFFTAKE_QTY','WT_DISCOUNT_PCT','WT_AVAILABILITY_PCT',
          'MONTHLY_AD_SOV','WT_AVG_PPU_X100']:
    df[c] = pd.to_numeric(df[c], errors='coerce')
df['DATE'] = pd.to_datetime(df['DATE'])
df['GRAMMAGE'] = df['GRAMMAGE'].astype(str).str.strip()

own = df[df['BRAND'].str.contains('24 Mantra', case=False, na=False)].copy()
own = own[own['WT_AVAILABILITY_PCT'].fillna(100) >= 50].copy()  # regular days approx

# Simple features
own['log_units'] = np.log(own['OFFTAKE_QTY'].clip(lower=0.1))
own['discount_pct'] = own['WT_DISCOUNT_PCT'].fillna(0).clip(0, 70)
own['log1p_discount'] = np.log1p(own['discount_pct'])
own['osa_rolling_7d'] = own['WT_AVAILABILITY_PCT'].fillna(50) / 100
own['log_ad_sov'] = np.log1p(own['MONTHLY_AD_SOV'].fillna(0))
own['rpi'] = 1.0
own['discount_surprise'] = 0.0
own['is_weekend'] = pd.to_datetime(own['DATE']).dt.dayofweek.isin([5, 6]).astype(int)
own['is_deep_promo'] = (own['discount_pct'] > 20).astype(int)

own['sku_city'] = own['PRODUCT_ID'].astype(str) + '__' + own['GRAMMAGE'] + '__' + own['GC_CITY']
own['category'] = own['TITLE'].str.extract(r'(Jaggery|Moong|Sunflower)', expand=False).fillna('Other')

formula = ("log_units ~ C(category) + discount_pct + log1p_discount "
           "+ osa_rolling_7d + log_ad_sov + rpi + discount_surprise "
           "+ is_weekend + is_deep_promo")

train = own[own['DATE'] <= '2025-12-21'].copy()

print("Fitting MixedLM with random slopes...")
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    model = smf.mixedlm(
        formula,
        data=train,
        groups=train["sku_city"],
        exog_re=train[["discount_pct"]],
    )
    result = model.fit(reml=True, method="lbfgs", maxiter=500)

print(f"Converged: {result.converged}")
print(f"Params (first 5): {dict(list(result.params.items())[:5])}")
print(f"\nrandom_effects type: {type(result.random_effects)}")

# Get one cell's random effects to see the structure
sample_cells = list(result.random_effects.keys())[:3]
for cell in sample_cells:
    re = result.random_effects[cell]
    print(f"\nCell: {cell}")
    print(f"  type(re): {type(re)}")
    print(f"  re: {re}")
    if hasattr(re, 'index'):
        print(f"  re.index: {list(re.index)}")
