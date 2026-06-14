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
