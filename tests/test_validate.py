"""The fail-loud input validation gate."""
import numpy as np
import pandas as pd
import pytest
import v4_config as cfg
from stage1_ingestion.validate import validate_columns, validate_quality

C = cfg.COL


def _good_panel(n_cells=3, n_days=30):
    rows = []
    for c in range(n_cells):
        for t in range(n_days):
            rows.append({
                C["product_id"]: "P1", C["grammage"]: "500g",
                C["city"]: f"City{c}", C["title"]: "Brand Jaggery 500g",
                C["date"]: pd.Timestamp("2025-06-01") + pd.Timedelta(days=t),
                C["offtake_qty"]: 30.0, C["mrp"]: 100.0, C["discount_pct"]: 10.0,
            })
    return pd.DataFrame(rows)


def test_missing_required_column_fails_loud():
    df = _good_panel().drop(columns=[C["mrp"]])
    with pytest.raises(ValueError) as e:
        validate_columns(df)
    assert C["mrp"] in str(e.value)


def test_good_columns_pass():
    validate_columns(_good_panel())   # should not raise


def test_empty_panel_fails():
    with pytest.raises(ValueError):
        validate_quality(_good_panel().iloc[0:0])


def test_single_cell_fails():
    with pytest.raises(ValueError):
        validate_quality(_good_panel(n_cells=1))


def test_soft_issues_warn_not_raise():
    df = _good_panel()
    df.loc[df.index[:5], C["discount_pct"]] = 150.0   # out of range
    df.loc[df.index[5:8], C["offtake_qty"]] = -3.0     # negatives
    issues = validate_quality(df)                       # must NOT raise
    assert any("discount" in i for i in issues)
    assert any("negative" in i for i in issues)


# ── Named data-quality checks (PepsiCo §2.1.1, val_16) — synthetic plants ────

def _flat_panel(n_days=60, start="2026-05-01", pids=("P0", "P1")):
    """Two flat-demand cells far from any configured festival window."""
    rows = []
    for pid in pids:
        for t in range(n_days):
            rows.append({
                C["product_id"]: pid, C["grammage"]: "500g", C["city"]: "CityA",
                C["title"]: "Brand Jaggery 500g",
                C["date"]: pd.Timestamp(start) + pd.Timedelta(days=t),
                C["offtake_qty"]: 30.0 + (t % 5), C["mrp"]: 100.0,
                C["discount_pct"]: 10.0,
            })
    return pd.DataFrame(rows)


def test_unexplained_spike_flagged_but_deep_promo_spike_excused():
    from stage1_ingestion.validate import _check_unexplained_spikes
    df = _flat_panel()
    df.loc[30, C["offtake_qty"]] = 900.0            # spike, NO promo -> suspect
    df.loc[95, C["offtake_qty"]] = 900.0            # spike WITH deep promo -> explained
    df.loc[95, C["discount_pct"]] = 35.0
    out = _check_unexplained_spikes(df)
    assert out and "1 of 2" in out[0], out          # exactly the promo-less one


def test_margin_price_consistency_flags():
    from stage1_ingestion.validate import _check_margin_price_consistency
    df = _flat_panel()
    df[C["price"]] = 90.0
    df[C["availability"]] = 95.0
    df.loc[3, C["price"]] = 120.0                    # above MRP
    df.loc[5, C["price"]] = 30.0                     # below 50% COGS proxy
    df.loc[5, C["discount_pct"]] = 70.0
    df.loc[7, C["discount_pct"]] = 40.0              # PRICE implies 10%, field says 40%
    df.loc[9, C["availability"]] = 0.0               # sold units at 0 availability
    out = _check_margin_price_consistency(df)
    assert any("above MRP" in i for i in out), out
    assert any("below" in i and "COGS" in i for i in out), out
    assert any("disagreeing" in i for i in out), out
    assert any("availability was 0" in i for i in out), out


def test_sku_identity_churn_flagged():
    from stage1_ingestion.validate import _check_sku_identity_continuity
    df = _flat_panel(n_days=80)
    last = df[C["date"]].max()
    # P0 vanishes 30 days before the end; P9 appears in the final 8 days.
    df = df[~((df[C["product_id"]] == "P0") &
              (df[C["date"]] > last - pd.Timedelta(days=30)))].copy()
    new = _flat_panel(n_days=8, start=str((last - pd.Timedelta(days=7)).date()), pids=("P9",))
    out = _check_sku_identity_continuity(pd.concat([df, new], ignore_index=True))
    assert out and "P0" in out[0] and "P9" in out[0], out


def test_named_checks_survive_missing_optional_columns():
    # No price / availability columns at all -> checks degrade quietly, never raise.
    df = _flat_panel()
    issues = validate_quality(df)                    # must NOT raise
    assert isinstance(issues, list)
