"""
Deep diagnostic of input data to understand why the elasticity model fails.
Investigates: MRP variation, PRICE variation, discount distribution,
              data grain, column meanings, and price-demand relationships.
"""
import pandas as pd
import numpy as np
import glob
import os

files = sorted(glob.glob(r'input_data/*.xlsx'))

for f in files:
    df = pd.read_excel(f)
    fname = os.path.basename(f)
    print(f"\n{'='*70}")
    print(f"FILE: {fname}")
    print(f"{'='*70}")
    print(f"Shape: {df.shape}")
    print(f"All columns: {list(df.columns)}")

    # Coerce numerics
    num_cols = ['MRP', 'PRICE', 'OFFTAKE_QTY', 'OFFTAKE_MRP',
                'WT_DISCOUNT_PCT', 'WT_AVAILABILITY_PCT', 'MONTHLY_AD_SOV',
                'WT_AVG_PPU_X100', 'Competitor Price', 'Relative Price Index']
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # Normalise grammage
    if 'GRAMMAGE' in df.columns:
        df['GRAMMAGE'] = df['GRAMMAGE'].astype(str).str.strip()

    # Own brand only
    own = df[df['BRAND'].str.contains('24 Mantra', case=False, na=False)].copy()
    print(f"Own-brand rows: {len(own):,}")

    # Per grammage analysis
    for grm, gdf in own.groupby('GRAMMAGE'):
        print(f"\n  --- Grammage: {grm} | rows: {len(gdf):,} ---")

        print(f"  MRP  : min={gdf['MRP'].min():.3f}  max={gdf['MRP'].max():.3f}  "
              f"std={gdf['MRP'].std():.3f}  unique={gdf['MRP'].nunique()}")
        print(f"  PRICE: min={gdf['PRICE'].min():.3f}  max={gdf['PRICE'].max():.3f}  "
              f"std={gdf['PRICE'].std():.3f}  unique={gdf['PRICE'].nunique()}")

        if 'WT_DISCOUNT_PCT' in gdf.columns:
            print(f"  WT_DISCOUNT_PCT: min={gdf['WT_DISCOUNT_PCT'].min():.2f}  "
                  f"max={gdf['WT_DISCOUNT_PCT'].max():.2f}  "
                  f"mean={gdf['WT_DISCOUNT_PCT'].mean():.2f}  "
                  f"std={gdf['WT_DISCOUNT_PCT'].std():.2f}")
            vals = gdf['WT_DISCOUNT_PCT'].dropna().round(1).value_counts().sort_index()
            print(f"  Discount value counts (all): {dict(vals)}")

        if 'OFFTAKE_QTY' in gdf.columns:
            print(f"  OFFTAKE_QTY: min={gdf['OFFTAKE_QTY'].min():.2f}  "
                  f"max={gdf['OFFTAKE_QTY'].max():.2f}  "
                  f"mean={gdf['OFFTAKE_QTY'].mean():.2f}  "
                  f"zeros={( gdf['OFFTAKE_QTY']==0).sum()}")

        if 'WT_AVG_PPU_X100' in gdf.columns:
            print(f"  WT_AVG_PPU_X100 (actual selling price x100): "
                  f"min={gdf['WT_AVG_PPU_X100'].min():.1f}  "
                  f"max={gdf['WT_AVG_PPU_X100'].max():.1f}  "
                  f"unique={gdf['WT_AVG_PPU_X100'].nunique()}")

        # Sample rows
        print(f"  Sample 5 rows (key columns):")
        sample_cols = [c for c in ['DATE','GRAMMAGE','GC_CITY','MRP','PRICE',
                                    'WT_DISCOUNT_PCT','OFFTAKE_QTY','WT_AVG_PPU_X100'] if c in gdf.columns]
        print(gdf[sample_cols].dropna(subset=['OFFTAKE_QTY']).head(5).to_string(index=False))

        # Check WT_AVG_PPU_X100 as actual price
        if 'WT_AVG_PPU_X100' in gdf.columns:
            ppu = gdf['WT_AVG_PPU_X100'] / 100
            print(f"\n  WT_AVG_PPU_X100/100 (true selling price?): "
                  f"min={ppu.min():.2f}  max={ppu.max():.2f}  "
                  f"std={ppu.std():.2f}  unique={ppu.nunique()}")

        # Correlation between price and qty
        valid = gdf[['PRICE', 'OFFTAKE_QTY', 'WT_DISCOUNT_PCT']].dropna()
        if len(valid) > 10:
            r_price_qty = valid['PRICE'].corr(valid['OFFTAKE_QTY'])
            r_disc_qty  = valid['WT_DISCOUNT_PCT'].corr(valid['OFFTAKE_QTY'])
            print(f"\n  Correlation PRICE vs QTY:    {r_price_qty:.3f}")
            print(f"  Correlation DISCOUNT vs QTY: {r_disc_qty:.3f}")

print("\n\nDONE")
