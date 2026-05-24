import pandas as pd, numpy as np
df = pd.read_excel(r'input_data/24 Mantra X Jaggery Powder 500G X 1 Year X BlinkIT.xlsx')
for c in ['MRP','PRICE','WT_DISCOUNT_PCT','OFFTAKE_QTY','WT_AVG_PPU_X100']:
    df[c] = pd.to_numeric(df[c], errors='coerce')
df['GRAMMAGE'] = df['GRAMMAGE'].astype(str).str.strip()
own = df[df['BRAND'].str.contains('24 Mantra', case=False, na=False)]
for grm, g in own.groupby('GRAMMAGE'):
    print(f"Grammage: {grm}  rows: {len(g)}")
    print(f"  MRP   unique={g['MRP'].nunique()}  min={g['MRP'].min():.2f}  max={g['MRP'].max():.2f}  std={g['MRP'].std():.2f}")
    print(f"  PRICE unique={g['PRICE'].nunique()}  min={g['PRICE'].min():.2f}  max={g['PRICE'].max():.2f}  std={g['PRICE'].std():.2f}")
    print(f"  WT_DISCOUNT_PCT  min={g['WT_DISCOUNT_PCT'].min():.2f}  max={g['WT_DISCOUNT_PCT'].max():.2f}  mean={g['WT_DISCOUNT_PCT'].mean():.2f}  std={g['WT_DISCOUNT_PCT'].std():.2f}")
    disc_vals = g['WT_DISCOUNT_PCT'].round(1).value_counts().sort_index()
    print(f"  Discount vals: {dict(disc_vals)}")
    print(f"  QTY  min={g['OFFTAKE_QTY'].min():.1f}  max={g['OFFTAKE_QTY'].max():.1f}  mean={g['OFFTAKE_QTY'].mean():.1f}")
    # Correlation
    valid = g[['PRICE','OFFTAKE_QTY','WT_DISCOUNT_PCT']].dropna()
    print(f"  Corr PRICE-QTY: {valid['PRICE'].corr(valid['OFFTAKE_QTY']):.3f}")
    print(f"  Corr DISC-QTY:  {valid['WT_DISCOUNT_PCT'].corr(valid['OFFTAKE_QTY']):.3f}")
    print()
