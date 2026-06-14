"""filter_own_brand: the onboarding-critical fail-loud guards."""
import pandas as pd
import pytest
import v4_config as cfg
from stage1_ingestion.ingest import filter_own_brand

C = cfg.COL


def _df(brands):
    return pd.DataFrame({C["brand"]: brands, C["product_id"]: range(len(brands))})


def test_matching_brand_survives():
    df = _df(["Acme Foods", "Rival Co", "Acme Foods"])
    out = filter_own_brand(df, ["acme"])
    assert len(out) == 2
    assert set(out[C["brand"]]) == {"Acme Foods"}


def test_zero_match_fails_loud_and_lists_brands(monkeypatch):
    df = _df(["Rival Co", "Other Brand"])
    with pytest.raises(ValueError) as e:
        filter_own_brand(df, ["acme"])
    assert "acme" in str(e.value)
    assert "Rival Co" in str(e.value) or "Other Brand" in str(e.value)


def test_word_boundary_excludes_glued_competitor(monkeypatch):
    monkeypatch.setattr(cfg, "STRICT_OWN_BRAND_MATCH", True)
    # 'sun' must match 'Sun' but NOT the glued competitor 'Sunfeast'/'Sundrop'
    df = _df(["Sun", "Sunfeast", "Sundrop", "Sun"])
    out = filter_own_brand(df, ["sun"])
    assert set(out[C["brand"]]) == {"Sun"}        # competitors correctly excluded


def test_over_match_common_word_brand_fails_loud_when_strict(monkeypatch):
    monkeypatch.setattr(cfg, "STRICT_OWN_BRAND_MATCH", True)
    # a generic word as the pattern matches two genuinely different brands
    df = _df(["Gold Winner", "Tata Gold"])
    with pytest.raises(ValueError) as e:
        filter_own_brand(df, ["gold"])
    assert "Winner" in str(e.value) or "Tata" in str(e.value)


def test_over_match_allowed_when_not_strict(monkeypatch):
    monkeypatch.setattr(cfg, "STRICT_OWN_BRAND_MATCH", False)
    df = _df(["Gold Winner", "Tata Gold"])
    out = filter_own_brand(df, ["gold"])      # warns, does not raise
    assert len(out) == 2


def test_competitor_sharing_descriptor_token_not_flagged(monkeypatch):
    # 'Organic India' shares only the generic descriptor 'organic' with the
    # pattern '24 mantra organic' — it's a competitor, must NOT be flagged.
    monkeypatch.setattr(cfg, "STRICT_OWN_BRAND_MATCH", True)
    df = _df(["24 Mantra Organic", "Organic India", "Organic India"])
    out = filter_own_brand(df, ["24 mantra organic", "24 mantra"])
    assert set(out[C["brand"]]) == {"24 Mantra Organic"}


def test_brand_spelled_two_ways_is_fine(monkeypatch):
    # the real 24 Mantra case: same brand, two spellings → NOT an over-match
    monkeypatch.setattr(cfg, "STRICT_OWN_BRAND_MATCH", True)
    df = _df(["24 Mantra Organic", "24 Mantra", "Rival"])
    out = filter_own_brand(df, ["24 mantra organic", "24 mantra"])
    assert len(out) == 2
    assert "Rival" not in set(out[C["brand"]])
