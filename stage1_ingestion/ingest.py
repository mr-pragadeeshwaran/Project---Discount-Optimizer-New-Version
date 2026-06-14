"""
Stage 1 — Data Ingestion.

Reads all Excel files from the sales data directory, combines them,
and produces raw DataFrames ready for Stage 2.

IMPORTANT: Each (PRODUCT_ID, GRAMMAGE, City, Date) is treated as a unique
cell. Grammage is normalised into a canonical string ('500g', '1kg', etc.)
so that mixed raw values (500, '500 g', '500g') are unified before any
grouping or deduplication.
"""
import os
import re
import glob
import unicodedata
import pandas as pd
import numpy as np
import v4_config as cfg


def ingest_all_sales() -> pd.DataFrame:
    """Load all Excel files from SALES_DATA_DIR into one combined DataFrame."""
    pattern = os.path.join(cfg.SALES_DATA_DIR, "*.xlsx")
    files = [f for f in glob.glob(pattern) if not os.path.basename(f).startswith("~")]

    if not files:
        raise FileNotFoundError(f"No .xlsx files found in {cfg.SALES_DATA_DIR}")

    print(f"  [Stage 1] Found {len(files)} data files")
    frames = []
    pid = cfg.COL["product_id"]
    for fpath in sorted(files):
        fname = os.path.basename(fpath)
        df = pd.read_excel(fpath)
        n_sku = df[pid].nunique() if pid in df.columns else "?"
        print(f"    {fname}: {len(df):,} rows, {n_sku} SKUs")
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)

    # Fail loud NOW if a required column is missing (clear message, not a
    # cryptic KeyError deep in the pipeline).
    from stage1_ingestion.validate import validate_columns
    validate_columns(combined)

    # Basic type coercion (coerce dates so unparseable values become NaT and
    # the friendly validate_quality check fires instead of a cryptic crash)
    C = cfg.COL
    combined[C["date"]] = pd.to_datetime(combined[C["date"]], errors="coerce")

    numeric_cols = [C["offtake_mrp"], C["offtake_qty"], C["price"], C["mrp"],
                    C["availability"], C["discount_pct"], C["ad_sov"],
                    C["competitor_price"]]
    for col in numeric_cols:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")

    # ── Normalise GRAMMAGE into a canonical string ─────────────────
    # Raw data has mixed types: 500 (int), '500 g' (str), '500g' (str),
    # '1 kg' (str), etc. Normalise all to clean strings like '500g', '1kg'.
    if C["grammage"] in combined.columns:
        combined[C["grammage"]] = combined[C["grammage"]].apply(_normalise_grammage)
        unique_gram = combined[C["grammage"]].unique().tolist()
        print(f"  [Stage 1] Grammages found (normalised): {unique_gram}")
    else:
        print(f"  [Stage 1] ⚠ No GRAMMAGE column — grammage not used in cell identity")

    # Detect category from title (dynamic: auto-derived unless keywords set)
    own_patterns = resolve_own_brand_patterns()
    combined["category"] = combined[C["title"]].apply(
        lambda t: _detect_category(t, own_patterns))

    # Deduplicate: keep last row per (SKU, Grammage, City, Date)
    # Grammage is included so 500g and 1kg variants are NEVER merged.
    dedup_keys = [C["product_id"], C["city"], C["date"]]
    if C["grammage"] in combined.columns:
        dedup_keys = [C["product_id"], C["grammage"], C["city"], C["date"]]
    before = len(combined)
    combined = combined.drop_duplicates(subset=dedup_keys, keep="last")
    if len(combined) < before:
        print(f"    Deduped: {before:,} → {len(combined):,} rows")

    # ── Filter to own-brand SKUs only ──────────────────────────────
    # Substring match with OVER/UNDER-match guards (see filter_own_brand).
    if C["brand"] in combined.columns and own_patterns:
        combined = filter_own_brand(combined, own_patterns)
    elif C["brand"] in combined.columns:
        print(f"  [Stage 1] ⚠ No brand patterns resolved — processing ALL brands "
              f"(set BRAND_NAME in v4_config.py)")
    else:
        print(f"  [Stage 1] ⚠ No BRAND column — processing all SKUs")

    sort_keys = [C["product_id"], C["city"], C["date"]]
    if C["grammage"] in combined.columns:
        sort_keys = [C["product_id"], C["grammage"], C["city"], C["date"]]
    combined = combined.sort_values(sort_keys).reset_index(drop=True)

    n_skus = combined[C["product_id"]].nunique()
    n_cities = combined[C["city"]].nunique()
    grp_keys = [C["product_id"], C["city"]]
    if C["grammage"] in combined.columns:
        grp_keys = [C["product_id"], C["grammage"], C["city"]]
    n_cells = combined.groupby(grp_keys).ngroups
    n_cats = combined["category"].nunique()
    print(f"  [Stage 1] Combined: {len(combined):,} rows | "
          f"{n_skus} PRODUCT_IDs × {n_cities} cities = {n_cells} cells | "
          f"{n_cats} categories")

    # ── Category quality check ─────────────────────────────────────
    # The per-category model needs ≥200 rows AND ≥2 cells per category, else
    # those cells fall back to a global default elasticity. Surface thin
    # auto-derived categories so the operator can merge them via keywords.
    cat_rows = combined["category"].value_counts()
    cells_per_cat = combined.groupby(grp_keys)["category"].first().value_counts()
    print(f"  [Stage 1] Categories (mode={getattr(cfg, 'CATEGORY_MODE', 'auto')}):")
    thin = []
    for cat in cat_rows.index:
        r = int(cat_rows.get(cat, 0)); c = int(cells_per_cat.get(cat, 0))
        is_thin = (r < 200) or (c < 2)
        if is_thin:
            thin.append(str(cat))
        print(f"      {str(cat)[:26]:26s} rows={r:>6,}  cells={c:>3}{'  ⚠ THIN' if is_thin else ''}")
    if thin:
        # measure the REAL exposure: share of cells that will fall back to the
        # global default elasticity because their category is too thin to model.
        n_thin_cells = int(sum(int(cells_per_cat.get(c, 0)) for c in thin))
        total_cells = int(cells_per_cat.sum())
        share = n_thin_cells / max(total_cells, 1)
        print(f"  [Stage 1] ⚠ Thin categories {thin} → {n_thin_cells}/{total_cells} cells "
              f"({share:.0%}) will use the GLOBAL DEFAULT elasticity (not data-driven). "
              f"To fix, set CATEGORY_MODE='keywords' + CATEGORY_KEYWORDS to merge them.")
        warn_share = float(getattr(cfg, "CATEGORY_DEFAULT_FALLBACK_WARN_SHARE", 0.30))
        if share > warn_share:
            print(f"  [Stage 1] ⚠⚠ {share:.0%} of cells (> {warn_share:.0%}) would be priced "
                  f"off the generic default — auto-categorisation likely fragmented for this "
                  f"brand. Strongly recommend CATEGORY_MODE='keywords' before trusting output.")

    # Over-broad / failed-detection bucket: one category (esp. Other/Unknown)
    # holding most rows means auto-derivation lumped unrelated products together.
    total_rows = int(cat_rows.sum())
    for cat in cat_rows.index:
        sh = int(cat_rows[cat]) / max(total_rows, 1)
        name = str(cat)
        if name in ("Other", "Unknown") and sh > 0.20:
            print(f"  [Stage 1] ⚠ '{name}' holds {sh:.0%} of rows — category detection likely "
                  f"failed (terse/non-Latin/brand-leak titles); these get ONE blended elasticity. "
                  f"Set CATEGORY_MODE='keywords' + CATEGORY_KEYWORDS to split them.")
        elif sh > 0.60 and len(cat_rows) > 1:
            print(f"  [Stage 1] ⚠ Category '{name[:26]}' pools {sh:.0%} of all rows — auto-detection "
                  f"may be over-broad; verify it isn't lumping unrelated products.")

    # Quality gate on the final own-brand panel (warns soft, fails hard)
    from stage1_ingestion.validate import validate_quality
    validate_quality(combined)
    return combined


def _normalise_grammage(raw) -> str:
    """
    Normalise raw grammage values into a clean canonical string.

    Examples:
        500        → '500g'
        '500 g'    → '500g'
        '500g'     → '500g'
        '1 kg'     → '1kg'
        '1kg'      → '1kg'
        1000       → '1000g'
        None / NaN → 'unknown'
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "unknown"
    s = str(raw).strip().lower()
    # Remove spaces between number and unit
    s = re.sub(r'\s+', '', s)
    # '1kg' or '1.5kg' → keep as-is
    if re.match(r'^[\d.]+kg$', s):
        return s
    # '500g' → keep as-is
    if re.match(r'^[\d.]+g$', s):
        return s
    # Bare number like 500 or 1000 → treat as grams
    if re.match(r'^[\d.]+$', s):
        num = float(s)
        if num >= 1000:
            # e.g. 1000 → 1kg
            kg = num / 1000
            return f"{int(kg) if kg == int(kg) else kg}kg"
        return f"{int(num) if num == int(num) else num}g"
    # Fallback: return cleaned string
    return s


def resolve_own_brand_patterns() -> list:
    """
    The own-brand match patterns. Uses OWN_BRAND_PATTERNS if set, else derives
    from BRAND_NAME — so onboarding a new client is usually a one-line change.
    """
    pats = [str(p).strip().lower() for p in getattr(cfg, "OWN_BRAND_PATTERNS", []) or []
            if str(p).strip()]
    if not pats:
        bn = str(getattr(cfg, "BRAND_NAME", "")).strip().lower()
        if bn:
            pats = [bn]
    return pats


_BRAND_DESCRIPTORS = {"foods", "food", "ltd", "limited", "pvt", "private", "co",
                      "company", "inc", "india", "brand", "brands", "organic",
                      "the", "group", "industries", "products"}


def _brand_tokens(s):
    return set(re.findall(r'[a-z0-9]+', str(s).lower()))


def _brand_matches(b: str, own_patterns: list) -> bool:
    """
    Own-brand iff a pattern appears as a WHOLE WORD/PHRASE (or its concatenated
    spelling) in the brand value. Word boundaries stop a short pattern from
    eating a different word — 'sun' matches 'Sun'/'Sun Foods' but NOT 'Sunfeast'.
    """
    for p in own_patterns:
        p = p.strip()
        if not p:
            continue
        if re.search(r'\b' + re.escape(p) + r'\b', b):
            return True
        cc = p.replace(" ", "")
        if cc and re.search(r'\b' + re.escape(cc) + r'\b', b):
            return True
    return False


def filter_own_brand(combined: pd.DataFrame, own_patterns: list) -> pd.DataFrame:
    """
    Keep only own-brand rows. Match is on WORD BOUNDARIES (a short BRAND_NAME
    can't silently absorb a competitor like 'Sunfeast'), with guards that FAIL
    LOUD on the two silent-onboarding failures:
      • OVER-match  — a pattern matched a brand value carrying a genuinely
        DIFFERENT token (e.g. 'gold' → 'Gold Winner' AND 'Tata Gold').
        Controlled by STRICT_OWN_BRAND_MATCH.
      • UNDER-match — a brand spelling that looks like the own brand but didn't
        match (would be silently dropped as a competitor).
    """
    C = cfg.COL
    brand_norm = combined[C["brand"]].astype(str).str.strip().str.lower()
    mask = brand_norm.apply(lambda b: _brand_matches(b, own_patterns))
    n_own = int(mask.sum()); n_comp = len(combined) - n_own

    if n_own == 0:
        found = sorted(combined[C["brand"]].dropna().astype(str).unique())[:25]
        raise ValueError(
            f"No own-brand rows matched patterns {own_patterns} "
            f"(from BRAND_NAME='{getattr(cfg, 'BRAND_NAME', '')}'). Brands present: "
            f"{found}. Set BRAND_NAME / OWN_BRAND_PATTERNS in v4_config.py to match.")

    kept = sorted(combined.loc[mask, C["brand"]].dropna().astype(str).str.strip().unique())
    print(f"  [Stage 1] Brand filter: keeping own brand ({getattr(cfg, 'BRAND_NAME', '')})")
    print(f"    Own brand rows: {n_own:,} | Competitor rows removed: {n_comp:,}")
    print(f"    Distinct brands KEPT as own-brand ({len(kept)}): {kept[:10]}"
          f"{' …' if len(kept) > 10 else ''}")

    # OVER-match guard: a kept brand carries a DIFFERENT identifying token after
    # removing the pattern tokens and generic descriptors. Passes "Acme Foods"
    # (residual {foods}=descriptor) and "24 Mantra Organic" (residual {}), but
    # catches 'gold' → 'Gold Winner'/'Tata Gold' (residuals {winner}/{tata}).
    pat_tokens = set().union(*[_brand_tokens(p) for p in own_patterns]) if own_patterns else set()
    def _absorbed(b):
        resid = {w for w in (_brand_tokens(b) - pat_tokens - _BRAND_DESCRIPTORS) if len(w) >= 3}
        return len(resid) > 0
    absorbed = sorted({b for b in kept if _absorbed(b)})
    if absorbed:
        msg = (f"Own-brand patterns {own_patterns} also matched brand value(s) {absorbed} "
               f"carrying a DIFFERENT brand token (a generic pattern absorbing competitors, "
               f"e.g. 'gold' → 'Tata Gold'). Tighten BRAND_NAME / OWN_BRAND_PATTERNS, or set "
               f"STRICT_OWN_BRAND_MATCH=False if these are genuinely yours.")
        if bool(getattr(cfg, "STRICT_OWN_BRAND_MATCH", True)):
            raise ValueError(msg)
        print(f"  [Stage 1] ⚠ {msg}")

    # PARTIAL UNDER-match guard: an unmatched brand that shares a DISTINCTIVE
    # own-brand token (not a generic descriptor like 'organic') and carries NO
    # different identifying token is a spelling the word-boundary match missed
    # (e.g. 'Mother-Dairy'). This excludes competitors like 'Organic India'
    # (shares only the descriptor 'organic') and 'Sunfeast' (own token).
    distinctive = pat_tokens - _BRAND_DESCRIPTORS
    suspicious = sorted({
        b for b in combined.loc[~mask, C["brand"]].dropna().astype(str).unique()
        if (_brand_tokens(b) & distinctive) and not _absorbed(b)
    })[:25]
    if suspicious:
        msg = (f"{len(suspicious)} brand spelling(s) look like the own brand but did "
               f"NOT match and would be DROPPED: {suspicious}. Add them to "
               f"OWN_BRAND_PATTERNS in v4_config.py, or fix the source data.")
        if bool(getattr(cfg, "STRICT_OWN_BRAND_MATCH", True)):
            raise ValueError(msg)
        print(f"  [Stage 1] ⚠ {msg}")

    return combined[mask].copy()


# size / pack tokens to strip when auto-deriving a category from a title.
# Bare ambiguous single letters (l, n, x) are intentionally EXCLUDED — a stray
# 'l' from '1l' is cleaned up later by the digit-strip + len>1 filter, but
# keeping them here risked eating real number+letter variant tokens.
_SIZE_RE = re.compile(
    r'\b[\d.]+\s*(?:kgs?|kg|gms?|gm|g|mls?|ml|ltrs?|litres?|liters?|ltr|'
    r'pcs?|pc|packs?|pack|combo|count|ct)\b', re.I)
# generic + common variant/adjective words that fragment a product type
# (e.g. "Refined Sunflower" vs "Sunflower Oil"). Operator can extend via config.
_GENERIC_WORDS = {"organic", "pack", "of", "combo", "value", "saver", "with",
                  "and", "the", "natural", "premium", "pure", "refined", "raw",
                  "gold", "double", "filtered", "toned", "full", "cold",
                  "pressed", "fresh", "classic", "regular", "special"}


def _norm_for_match(s: str) -> str:
    """Lowercase + collapse hyphen/underscore/slash/dot/space noise for matching."""
    s = str(s).lower()
    s = re.sub(r'[\-_/.]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _auto_category(title: str, brand_patterns: list) -> str:
    """
    Derive a category from the title with NO hardcoded keywords: strip the brand
    name (spelling-variant tolerant) and pack/size tokens, then keep the first
    couple of content words — the core product type. Unicode-aware so non-Latin
    titles keep their own words instead of collapsing to "Other".
    e.g. "24 Mantra Organic Jaggery Powder 500G" → "Jaggery Powder";
         "MotherDairy Toned Milk 500ml" → "Milk" (brand stripped even concatenated).
    """
    if not isinstance(title, str) or not title.strip():
        return "Unknown"
    # fold Latin accents (café→cafe) but keep non-Latin scripts intact
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    t = _norm_for_match(folded)
    for bp in brand_patterns:
        bp_n = _norm_for_match(bp)
        if not bp_n:
            continue
        t = re.sub(r'\b' + re.escape(bp_n) + r'\b', " ", t)            # spaced form
        t = re.sub(r'\b' + re.escape(bp_n.replace(" ", "")) + r'\b', " ", t)  # concatenated
    t = _SIZE_RE.sub(" ", t)
    t = re.sub(r'[\d\W_]+', " ", t, flags=re.UNICODE)   # drop digits/punct, keep unicode letters
    stop = _GENERIC_WORDS | {str(w).lower() for w in getattr(cfg, "CATEGORY_EXTRA_STOPWORDS", [])}
    words = [w for w in t.split() if w not in stop and len(w) > 1]
    if not words:
        return "Other"
    return " ".join(words[:2]).title()        # first 2 content words = the type


def _detect_category(title: str, brand_patterns: list) -> str:
    """Category per title — auto-derived by default, keyword map if configured."""
    if getattr(cfg, "CATEGORY_MODE", "auto") == "keywords":
        if not isinstance(title, str):
            return "Unknown"
        tl = title.lower()
        for cat, keywords in cfg.CATEGORY_KEYWORDS.items():
            if all(kw in tl for kw in keywords):
                return cat
        return "Other"
    return _auto_category(title, brand_patterns)


def load_master_costs() -> pd.DataFrame:
    """Load or generate default cost data per SKU."""
    master_path = os.path.join(cfg.MASTER_DATA_DIR, "sku_costs.csv")
    if os.path.exists(master_path):
        print(f"  [Stage 1] Loading master costs from {master_path}")
        return pd.read_csv(master_path)

    # Generate defaults from data
    print(f"  [Stage 1] No master cost file found — using configurable defaults")
    print(f"    COGS: {cfg.DEFAULT_COGS_PCT*100:.0f}% of MRP | "
          f"Commission: {cfg.DEFAULT_COMMISSION_PCT*100:.0f}% | "
          f"Fulfillment: ₹{cfg.DEFAULT_FULFILLMENT_FEE}")
    return pd.DataFrame()  # Empty = use defaults in Stage 6


def load_event_calendar() -> pd.DataFrame:
    """Build event/festival calendar from config."""
    rows = []
    # Festival dates
    for date_str, name in cfg.FESTIVAL_DATES.items():
        dt = pd.Timestamp(date_str)
        for offset in range(-cfg.FESTIVAL_WINDOW_DAYS, cfg.FESTIVAL_WINDOW_DAYS + 1):
            rows.append({
                "date": dt + pd.Timedelta(days=offset),
                "event_name": name,
                "event_type": "festival",
            })

    # Platform events (date ranges)
    for (start, end), name in cfg.PLATFORM_EVENT_WINDOWS.items():
        for dt in pd.date_range(start, end):
            rows.append({
                "date": dt,
                "event_name": name,
                "event_type": "platform_sale",
            })

    if rows:
        cal = pd.DataFrame(rows).drop_duplicates(subset=["date", "event_name"])
        print(f"  [Stage 1] Event calendar: {len(cal)} event-days loaded")
        return cal
    return pd.DataFrame(columns=["date", "event_name", "event_type"])


if __name__ == "__main__":
    df = ingest_all_sales()
    print(df.head())
    cal = load_event_calendar()
    print(f"\nCalendar: {len(cal)} rows")
