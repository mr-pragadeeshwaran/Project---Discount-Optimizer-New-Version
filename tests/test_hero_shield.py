"""
Hero-SKU protection in the weekly tracker.

STRATEGIC_SKUS is the brand's "never auto-cut this, whatever the model says"
list. It is matched against the plan's product_id — and the two sides do NOT
share a dtype: the plan's column parses as int64, while ids configured in
config/settings.* arrive as strings. A raw isin() therefore matches nothing and
protects nothing, silently. These tests pin the normalized match.
"""
import os
import sys
import pandas as pd
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts", "tracker"))

import weekly_tracker as wt


def _plan_csv(tmp_path, product_ids):
    """A minimal all_cells.csv: every row a cuttable waste cell."""
    d = pd.DataFrame({
        "cell_id": [f"{p}_500g_Delhi-NCR" for p in product_ids],
        "product_id": product_ids,
        "city": ["Delhi-NCR"] * len(product_ids),
        "title": [f"SKU {p}" for p in product_ids],
        "category": ["Dal & Pulses"] * len(product_ids),
        "bucket": ["c_waste_cut"] * len(product_ids),
        "cur_disc": [20.0] * len(product_ids),
        "tgt_disc": [8.0] * len(product_ids),
        "cur_price": [100.0] * len(product_ids),
        "mrp": [125.0] * len(product_ids),
        "cur_units_wk": [100.0] * len(product_ids),
        "tgt_units_wk": [99.0] * len(product_ids),
        "net_gain_mo": [5000.0] * len(product_ids),
        "disc_spend_mo": [2000.0] * len(product_ids),
        "marginal_roas": [0.3] * len(product_ids),
        "confidence": ["High"] * len(product_ids),
        "decision_reason": ["waste"] * len(product_ids),
        "reinvest_headroom_pp": [0.0] * len(product_ids),
    })
    p = tmp_path / "all_cells.csv"
    d.to_csv(p, index=False)
    return str(p)


def _actions(tmp_path, product_ids, heroes, monkeypatch):
    import v4_config
    monkeypatch.setattr(v4_config, "STRATEGIC_SKUS", heroes, raising=False)
    df = wt.build_plan_df(_plan_csv(tmp_path, product_ids))
    return dict(zip(df["product_id"].map(wt._pid_key), df["suggested_disc"]))


# ── the dtype trap ─────────────────────────────────────────────────────────
def test_hero_ids_as_strings_protect_int_product_ids(tmp_path, monkeypatch):
    """The bug: settings-file heroes are strings, the plan's ids are int64."""
    acts = _actions(tmp_path, [496799, 521140, 108382], ["496799", "521140"], monkeypatch)
    assert acts["496799"] == 20.0      # hero — discount untouched
    assert acts["521140"] == 20.0      # hero — discount untouched
    assert acts["108382"] == 8.0       # not a hero — cut to target


def test_hero_ids_as_ints_still_work(tmp_path, monkeypatch):
    """v4_config's own comment suggests ints — that must keep working."""
    acts = _actions(tmp_path, [496799, 108382], [496799], monkeypatch)
    assert acts["496799"] == 20.0
    assert acts["108382"] == 8.0


def test_hero_ids_survive_a_float_parsed_plan(tmp_path, monkeypatch):
    """A plan whose id column parsed as float ('532393.0') must still match."""
    acts = _actions(tmp_path, [496799.0, 108382.0], ["496799"], monkeypatch)
    assert acts["496799"] == 20.0
    assert acts["108382"] == 8.0


def test_no_heroes_configured_cuts_everything(tmp_path, monkeypatch):
    acts = _actions(tmp_path, [496799, 108382], [], monkeypatch)
    assert acts["496799"] == 8.0
    assert acts["108382"] == 8.0


def test_pid_key_normalizes_every_shape():
    assert wt._pid_key(532393) == "532393"
    assert wt._pid_key(532393.0) == "532393"
    assert wt._pid_key("532393.0") == "532393"
    assert wt._pid_key("532393") == "532393"
    assert wt._pid_key(" 532393 ") == "532393"


# ── a broken settings file must not silently drop hero protection ──────────
def test_broken_settings_file_raises_rather_than_unprotecting(tmp_path, monkeypatch):
    """If config/settings.* is invalid the run must STOP. Falling back to
    'no heroes' would quietly make every flagship SKU cuttable."""
    import settings_loader as sl
    monkeypatch.setattr(sl, "CSV_PATH", str(tmp_path / "settings.csv"))
    monkeypatch.setattr(sl, "XLSX_PATH", str(tmp_path / "settings.xlsx"))
    monkeypatch.setattr(sl, "FESTIVALS_CSV", str(tmp_path / "festivals.csv"))
    monkeypatch.setattr(sl, "EVENTS_CSV", str(tmp_path / "events.csv"))
    (tmp_path / "settings.csv").write_text("key,value\nSTRATEGIC_SKUS_TYPO,1\n",
                                           encoding="utf-8")
    with pytest.raises(sl.SettingsError):
        sl.apply_to({})
