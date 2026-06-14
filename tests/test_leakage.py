"""Leakage decomposition: pull-forward detected, flat cells flagged no_promo."""
import numpy as np
import pandas as pd
import v4_config as cfg
from stage8_output.leakage import decompose_leakage

C = cfg.COL


def _row(city, t, units, disc):
    return {
        C["product_id"]: "P1", C["grammage"]: "500g", C["city"]: city,
        C["date"]: pd.Timestamp("2025-06-01") + pd.Timedelta(days=int(t)),
        C["offtake_qty"]: float(units), "discount_pct": float(disc),
        "is_regular_day": 1, "category": "Jaggery Powder",
    }


def test_pull_forward_detected_on_constructed_promo():
    # Deterministic panel designed to land in the INTERIOR (φ strictly 0<φ<1),
    # NOT the clipped/over-attributed corner. Normal units are constant so
    # high-discount normal days produce ~0 uplift (no phantom episodes); only
    # the real promo (units 60 vs baseline 30) clears the absolute floor.
    rows = []
    for t in range(40):                       # 40 normal days, units flat at 30
        rows.append(_row("CityA", t, 30, [5, 8, 11, 14][t % 4]))
    for t in range(40, 45):                   # 5-day promo: deep discount, unit spike
        rows.append(_row("CityA", t, 60, 30))
    for t in range(45, 55):                   # 10-day moderate post-promo dip (24 < 30)
        rows.append(_row("CityA", t, 24, 8))
    lk = decompose_leakage(pd.DataFrame(rows))
    row = lk[lk["cell_id"] == "P1_500g_CityA"].iloc[0]
    assert row["n_episodes"] == 1                     # exactly the constructed promo
    assert 0.2 < row["pull_forward"] < 0.95           # interior, not a clipped 1.0
    assert 0.0 < row["true_incremental_frac"] < 1.0   # genuinely netted, not zeroed
    assert "_over_attributed" not in row["leakage_confidence"]


def test_always_promo_chronically_deep_discounter():
    # constant DEEP discount → can't form a clean baseline → 'always_promo'
    rows = [_row("CityC", t, 25, 30) for t in range(40)]
    lk = decompose_leakage(pd.DataFrame(rows))
    row = lk[lk["cell_id"] == "P1_500g_CityC"].iloc[0]
    assert row["leakage_confidence"] == "always_promo"
    assert row["true_incremental_frac"] == 1.0


def test_flat_cell_is_no_promo():
    # constant discount, constant units → no promo variation to measure
    rows = [_row("CityB", t, 25, 10) for t in range(40)]
    lk = decompose_leakage(pd.DataFrame(rows))
    row = lk[lk["cell_id"] == "P1_500g_CityB"].iloc[0]
    assert row["leakage_confidence"] in ("no_promo", "no_variation")
    assert row["true_incremental_frac"] == 1.0


def test_cannibalization_only_within_same_category_city():
    # two cells, same category + city → siblings; a flat sibling adds no κ
    rows = []
    for t in range(40):
        rows.append(_row("CityA", t, 30 + (t % 5), 5 + (t % 11)))
    for t in range(40, 45):
        rows.append(_row("CityA", t, 60, 30))
    for t in range(45, 55):
        rows.append(_row("CityA", t, 12, 8))
    # sibling: different product, same category+city, steady (no dip)
    sib = []
    for t in range(55):
        r = _row("CityA", t, 20, 10); r[C["product_id"]] = "P2"
        sib.append(r)
    lk = decompose_leakage(pd.DataFrame(rows + sib))
    focal = lk[lk["cell_id"] == "P1_500g_CityA"].iloc[0]
    # sibling exists ⇒ confidence is NOT '_no_siblings'; steady sibling ⇒ κ≈0
    assert "_no_siblings" not in focal["leakage_confidence"]
    assert focal["cannibalization"] == 0.0
