"""
Settings-file overrides (settings_loader.py).

Every script in the repo does `import v4_config as cfg`, so a settings file
that parses wrong is a whole-system defect. These tests pin the two things
that matter: correct values get through, and wrong ones fail LOUD rather
than quietly repricing a brand's catalogue.
"""
import os
import pytest

import settings_loader as sl


# ── helpers ────────────────────────────────────────────────────────────────
@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """Point the loader at a throwaway config dir for the duration of a test."""
    monkeypatch.setattr(sl, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(sl, "XLSX_PATH", str(tmp_path / "settings.xlsx"))
    monkeypatch.setattr(sl, "CSV_PATH", str(tmp_path / "settings.csv"))
    monkeypatch.setattr(sl, "FESTIVALS_CSV", str(tmp_path / "festivals.csv"))
    monkeypatch.setattr(sl, "EVENTS_CSV", str(tmp_path / "platform_events.csv"))
    return tmp_path


def write_settings(cfgdir, body, name="settings.csv"):
    (cfgdir / name).write_text("key,value\n" + body, encoding="utf-8")


# ── no file ⇒ defaults untouched ───────────────────────────────────────────
def test_no_file_leaves_namespace_untouched(cfgdir):
    ns = {"SAVINGS_TARGET_MONTHLY_INR": 500_000}
    assert sl.apply_to(ns) == {}
    assert ns["SAVINGS_TARGET_MONTHLY_INR"] == 500_000
    assert sl.STATE["source"] is None


# ── the ask: the monthly target comes from the file ────────────────────────
def test_monthly_target_overridden_from_file(cfgdir):
    write_settings(cfgdir, "SAVINGS_TARGET_MONTHLY_INR,400000\n")
    ns = {"SAVINGS_TARGET_MONTHLY_INR": 500_000}
    sl.apply_to(ns)
    assert ns["SAVINGS_TARGET_MONTHLY_INR"] == 400_000
    assert sl.STATE["overrides"]["SAVINGS_TARGET_MONTHLY_INR"] == 400_000


def test_blank_value_keeps_the_default(cfgdir):
    write_settings(cfgdir, "SAVINGS_TARGET_MONTHLY_INR,\nTARGET_DISCOUNT_PCT,8\n")
    ns = {"SAVINGS_TARGET_MONTHLY_INR": 500_000, "TARGET_DISCOUNT_PCT": 10.0}
    sl.apply_to(ns)
    assert ns["SAVINGS_TARGET_MONTHLY_INR"] == 500_000     # blank ⇒ default
    assert ns["TARGET_DISCOUNT_PCT"] == 8.0


def test_excel_float_target_becomes_int(cfgdir):
    """Excel writes 400000 as 400000.0 — that must not become a float target."""
    write_settings(cfgdir, "SAVINGS_TARGET_MONTHLY_INR,400000.0\n")
    ns = {}
    sl.apply_to(ns)
    assert ns["SAVINGS_TARGET_MONTHLY_INR"] == 400_000
    assert isinstance(ns["SAVINGS_TARGET_MONTHLY_INR"], int)


def test_thousands_separators_accepted(cfgdir):
    write_settings(cfgdir, 'SAVINGS_TARGET_MONTHLY_INR,"450,000"\n')
    ns = {}
    sl.apply_to(ns)
    assert ns["SAVINGS_TARGET_MONTHLY_INR"] == 450_000


# ── fail loud: typos ───────────────────────────────────────────────────────
def test_unknown_key_is_rejected(cfgdir):
    write_settings(cfgdir, "SAVINGS_TARGET_MONTHY_INR,400000\n")   # typo: MONTHY
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    assert "SAVINGS_TARGET_MONTHY_INR" in str(e.value)
    assert "SAVINGS_TARGET_MONTHLY_INR" in str(e.value)            # suggests the real one


def test_every_problem_is_reported_at_once(cfgdir):
    write_settings(cfgdir, "NOT_A_KEY,1\nTARGET_TIMELINE_WEEKS,twelve\n")
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    msg = str(e.value)
    assert "NOT_A_KEY" in msg and "TARGET_TIMELINE_WEEKS" in msg
    assert "2 problem(s)" in msg


def test_nothing_is_applied_when_the_file_has_an_error(cfgdir):
    """A partially-good file must change NOTHING — no half-applied config."""
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,8\nNOT_A_KEY,1\n")
    ns = {"TARGET_DISCOUNT_PCT": 10.0}
    with pytest.raises(sl.SettingsError):
        sl.apply_to(ns)
    assert ns["TARGET_DISCOUNT_PCT"] == 10.0


# ── fail loud: the percent-vs-fraction footgun ─────────────────────────────
def test_budget_cap_percent_mixup_is_rejected_with_a_hint(cfgdir):
    """12 meaning '12%' would silently become a 1200% cap = cuts never fire."""
    write_settings(cfgdir, "DEFAULT_BUDGET_PCT_CAP,12\n")
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    msg = str(e.value)
    assert "FRACTION" in msg
    assert "0.12" in msg          # tells them what to write instead


def test_budget_cap_fraction_is_accepted(cfgdir):
    write_settings(cfgdir, "DEFAULT_BUDGET_PCT_CAP,0.10\n")
    ns = {}
    sl.apply_to(ns)
    assert ns["DEFAULT_BUDGET_PCT_CAP"] == 0.10


def test_percent_over_100_is_rejected(cfgdir):
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,120\n")
    with pytest.raises(sl.SettingsError):
        sl.apply_to({})


# ── lists: hero SKUs ───────────────────────────────────────────────────────
def test_hero_skus_parse_and_survive_excel_float_ids(cfgdir):
    write_settings(cfgdir, "STRATEGIC_SKUS,532393.0 | 496799 | 521140\n")
    ns = {}
    sl.apply_to(ns)
    assert ns["STRATEGIC_SKUS"] == ["532393", "496799", "521140"]   # no '.0'


def test_hero_skus_none_means_empty_list(cfgdir):
    write_settings(cfgdir, "STRATEGIC_SKUS,none\n")
    ns = {"STRATEGIC_SKUS": ["1", "2"]}
    sl.apply_to(ns)
    assert ns["STRATEGIC_SKUS"] == []


def test_brand_patterns_accept_commas(cfgdir):
    write_settings(cfgdir, "OWN_BRAND_PATTERNS,24 Mantra Organic | 24 Mantra\n")
    ns = {}
    sl.apply_to(ns)
    assert ns["OWN_BRAND_PATTERNS"] == ["24 Mantra Organic", "24 Mantra"]


def test_yes_no_parses(cfgdir):
    write_settings(cfgdir, "STRICT_OWN_BRAND_MATCH,no\n")
    ns = {"STRICT_OWN_BRAND_MATCH": True}
    sl.apply_to(ns)
    assert ns["STRICT_OWN_BRAND_MATCH"] is False


# ── calendars ──────────────────────────────────────────────────────────────
def test_festivals_file_replaces_the_code_calendar(cfgdir):
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,9\n")
    (cfgdir / "festivals.csv").write_text(
        "date,event\n2026-11-08,Diwali\n2026-08-15,Independence Day\n", encoding="utf-8")
    ns = {"FESTIVAL_DATES": {"2025-01-14": "Makar Sankranti"}}
    sl.apply_to(ns)
    assert ns["FESTIVAL_DATES"] == {"2026-11-08": "Diwali",
                                    "2026-08-15": "Independence Day"}


def test_no_festivals_file_keeps_the_code_calendar(cfgdir):
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,9\n")
    ns = {"FESTIVAL_DATES": {"2025-01-14": "Makar Sankranti"}}
    sl.apply_to(ns)
    assert ns["FESTIVAL_DATES"] == {"2025-01-14": "Makar Sankranti"}


def test_bad_festival_date_fails_loud(cfgdir):
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,9\n")
    (cfgdir / "festivals.csv").write_text("date,event\n08-11-2026,Diwali\n",
                                          encoding="utf-8")
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    assert "YYYY-MM-DD" in str(e.value)


def test_platform_event_window_backwards_fails_loud(cfgdir):
    write_settings(cfgdir, "TARGET_DISCOUNT_PCT,9\n")
    (cfgdir / "platform_events.csv").write_text(
        "start,end,event\n2026-10-06,2026-09-27,BBD\n", encoding="utf-8")
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    assert "before it" in str(e.value)


# ── malformed files ────────────────────────────────────────────────────────
def test_missing_columns_is_explained(cfgdir):
    (cfgdir / "settings.csv").write_text("name,setting\nfoo,1\n", encoding="utf-8")
    with pytest.raises(sl.SettingsError) as e:
        sl.apply_to({})
    assert "'key' and 'value'" in str(e.value)


# ── template round-trip: what we hand out must be what we accept ───────────
def test_csv_template_is_accepted_by_the_validator():
    ok, msg = sl.validate_bytes("settings.csv", sl.template_csv().encode("utf-8"))
    assert ok, msg


def test_xlsx_template_is_accepted_by_the_validator():
    ok, msg = sl.validate_bytes("settings.xlsx", sl.template_xlsx_bytes())
    assert ok, msg


def test_xlsx_template_round_trips_every_registry_key(cfgdir):
    """The generated workbook, fed back in, must reproduce the live config —
    proving the template lists the real keys with usable values."""
    (cfgdir / "settings.xlsx").write_bytes(sl.template_xlsx_bytes())
    ns = {}
    applied = sl.apply_to(ns)
    import v4_config as cfg
    for key, typ, _s, _d in sl.REGISTRY:
        live = getattr(cfg, key)
        if isinstance(live, (list, tuple)):
            assert applied.get(key, []) == list(live), key
        elif typ == "integer":
            assert applied[key] == live, key
        elif typ in ("number", "fraction", "percent"):
            assert abs(applied[key] - live) < 1e-9, key
        else:
            assert applied[key] == live, key


def test_template_covers_the_festival_calendar(cfgdir):
    (cfgdir / "settings.xlsx").write_bytes(sl.template_xlsx_bytes())
    ns = {}
    sl.apply_to(ns)
    import v4_config as cfg
    assert ns["FESTIVAL_DATES"] == cfg.FESTIVAL_DATES
    assert ns["PLATFORM_EVENT_WINDOWS"] == cfg.PLATFORM_EVENT_WINDOWS


# ── upload flow ────────────────────────────────────────────────────────────
def test_validate_rejects_a_bad_upload_without_touching_disk(cfgdir):
    ok, msg = sl.validate_bytes("settings.csv", b"key,value\nNOT_A_KEY,1\n")
    assert not ok and "NOT_A_KEY" in msg
    assert not os.path.exists(sl.CSV_PATH)


def test_validate_rejects_a_wrong_extension():
    ok, msg = sl.validate_bytes("settings.txt", b"key,value\n")
    assert not ok and ".csv or .xlsx" in msg


def test_install_writes_only_on_a_valid_file(cfgdir):
    ok, _ = sl.install_bytes("settings.csv", b"key,value\nBAD_KEY,1\n")
    assert not ok
    assert not os.path.exists(sl.CSV_PATH)

    ok, msg = sl.install_bytes("whatever-the-user-named-it.csv",
                               b"key,value\nTARGET_DISCOUNT_PCT,7\n")
    assert ok, msg
    assert os.path.exists(sl.CSV_PATH)          # always the fixed path, never theirs


def test_install_keeps_one_source_of_truth(cfgdir):
    sl.install_bytes("settings.csv", b"key,value\nTARGET_DISCOUNT_PCT,7\n")
    assert os.path.exists(sl.CSV_PATH)
    ok, msg = sl.install_bytes("settings.xlsx", sl.template_xlsx_bytes())
    assert ok, msg
    assert os.path.exists(sl.XLSX_PATH)
    assert not os.path.exists(sl.CSV_PATH)      # the stale csv is removed
