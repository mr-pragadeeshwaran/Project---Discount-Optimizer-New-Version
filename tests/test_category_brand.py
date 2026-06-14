"""Dynamic brand + category detection (the onboarding-critical logic)."""
import v4_config as cfg
from stage1_ingestion.ingest import (
    _auto_category, _detect_category, resolve_own_brand_patterns)

BRAND_PATTERNS = ["24 mantra organic", "24 mantra"]


def test_auto_category_strips_brand_and_size():
    assert _auto_category("24 Mantra Organic Jaggery Powder 500G", BRAND_PATTERNS) == "Jaggery Powder"
    assert _auto_category("24 Mantra Organic Sunflower Oil 1L", BRAND_PATTERNS) == "Sunflower Oil"
    assert _auto_category("24 Mantra Organic Moong Dal 500 g", BRAND_PATTERNS) == "Moong Dal"


def test_auto_category_groups_pack_variants_together():
    # 500g and 1kg of the same product must land in the SAME category (pooling)
    c1 = _auto_category("Acme Toor Dal 500g", ["acme"])
    c2 = _auto_category("Acme Toor Dal 1kg", ["acme"])
    assert c1 == c2 == "Toor Dal"


def test_auto_category_works_for_an_unknown_brand_no_keywords():
    # No hardcoded keyword needed — derives from the title for any brand
    assert _auto_category("Tata Sampann Besan 1Kg", ["tata sampann"]) == "Besan"


def test_auto_category_handles_nonstring():
    assert _auto_category(None, BRAND_PATTERNS) == "Unknown"


def test_resolve_own_patterns_uses_explicit_then_brand_name(monkeypatch):
    monkeypatch.setattr(cfg, "OWN_BRAND_PATTERNS", ["Foo", "Foo Brand"])
    assert resolve_own_brand_patterns() == ["foo", "foo brand"]
    # empty list ⇒ derive from BRAND_NAME
    monkeypatch.setattr(cfg, "OWN_BRAND_PATTERNS", [])
    monkeypatch.setattr(cfg, "BRAND_NAME", "Acme Foods")
    assert resolve_own_brand_patterns() == ["acme foods"]


def test_keyword_mode_overrides_auto(monkeypatch):
    monkeypatch.setattr(cfg, "CATEGORY_MODE", "keywords")
    monkeypatch.setattr(cfg, "CATEGORY_KEYWORDS", {"Jaggery": ["jaggery"]})
    assert _detect_category("Some Brand Jaggery Powder 500g", []) == "Jaggery"
    assert _detect_category("Some Brand Mustard Oil 1L", []) == "Other"


def test_concatenated_or_hyphenated_brand_is_stripped():
    # brand spelled differently than BRAND_NAME must still strip → same category
    bp = ["24 mantra organic", "24 mantra"]
    assert _auto_category("24Mantra Sunflower Oil 1L", bp) == "Sunflower Oil"
    assert _auto_category("24-Mantra Sunflower Oil 1L", bp) == "Sunflower Oil"


def test_accented_and_nonlatin_titles_survive():
    # Latin accents fold; non-Latin scripts are kept (not collapsed to 'Other')
    assert _auto_category("Acme Café Latte 200ml", ["acme"]) == "Cafe Latte"
    assert _auto_category("कोई नमक 1kg", []) != "Other"


def test_brand_or_size_only_title_is_other():
    assert _auto_category("24 Mantra Organic 500g", ["24 mantra organic"]) == "Other"


def test_extra_stopwords_merge_variants(monkeypatch):
    monkeypatch.setattr(cfg, "CATEGORY_EXTRA_STOPWORDS", ["sona", "masuri"])
    # with sona/masuri stripped, the rice variants collapse to the type
    assert _auto_category("Acme Sona Masuri Rice 5kg", ["acme"]) == "Rice"


def test_shipped_keywords_yield_exactly_three_groups(monkeypatch):
    monkeypatch.setattr(cfg, "CATEGORY_MODE", "keywords")  # use the REAL cfg.CATEGORY_KEYWORDS
    cases = {
        "24 Mantra Organic Jaggery Powder 500G": "Jaggery",
        "24 Mantra Organic Moong Dal 500g": "Moong Dal",
        "24 Mantra Organic Sunflower Oil 1L": "Sunflower Oil",
        "24 Mantra Organic Toor Dal 1kg": "Other",
    }
    for title, expected in cases.items():
        assert _detect_category(title, []) == expected


def test_auto_default_24mantra_snapshot():
    # pins the live auto grouping for the real product line (catches heuristic drift)
    bp = ["24 mantra organic", "24 mantra"]
    assert _auto_category("24 Mantra Organic Jaggery Powder 500G", bp) == "Jaggery Powder"
    assert _auto_category("24 Mantra Organic Moong Dal 500g", bp) == "Moong Dal"
    assert _auto_category("24 Mantra Organic Sunflower Oil 1L", bp) == "Sunflower Oil"
