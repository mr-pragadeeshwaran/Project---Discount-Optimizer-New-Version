# Cross-Price v2 — decomposed matrix E + competitor interaction (CHALLENGER)

*Run `20260705_161703` · elasticity source: bayes · champion files untouched — consumers switch only via an explicit future config flag.*

## Verdict: ADOPT-READY, but note: the similarity decomposition failed its holdout gate, so the shipped cross block is the champion's own uniform split — the NET new content is the assembled matrix E and the measured competitor columns

- own_diag_all_negative: **True**
- cross_signs_match_champion: **True**
- frac_nonneg_cross: **0.77**
- champion_frac_pos_cross_by_category: **0.947**
- decomposition_estimated: **True**
- decomposition_beats_uniform_on_holdout: **False**
- shipped_weights: **uniform_fallback**
- holdout_no_degradation_of_shipped_matrix: **True**
- all_pass: **True**

## 1. What changed vs the champion (price_06)

The champion splits each category's total cross response **uniformly** over siblings. The challenger re-weights the SAME total mass by similarity (estimated on train weeks, 5 holdout weeks):

- m_family = **0.10** (different base product damps to this fraction)
- m_size   = **0.20** (different size bucket, ratio band 0.75–1.33)
- m_ppk    = **0.02** (different price-per-kg tier, band 0.80–1.25)
- Holdout weighted SSE: decomposed 39.55 vs uniform 39.46 → **-0.22%** (positive = decomposition fits held-out weeks better).
- **The decomposition LOST on holdout — it overfits the training weeks.** Honest call: the within-category cross signal in this data is too weak to support similarity re-weighting, so `cross_price_v2.csv` ships the SAFE uniform split (identical numbers to the champion's cross_price.csv). The estimated m's above are reported for the receipt, not used.
- Scale note: per-SKU total substitution mass is preserved exactly (sum of w_ij = 1), so portfolio conclusions from the champion (e.g. the cannibalization honesty check) cannot flip either way.

## 2. The full matrix E (price_12)

- n_skus: 84
- n_cities: 11
- n_own_diag: 585
- n_within_cat_cross: 2594
- n_comp_cols: 585
- own_brand_matrix_cells_all_cities: 33431
- nonzero_own_brand_entries: 3179
- fill_pct_own_brand: 9.51
- cross_category_entries: structural zeros (never estimated — stated honestly)

Honest structure statement: own-brand **cross-category** entries are ZERO by construction — they have never been estimated for this brand and this challenger does not invent them. Competitor DEMAND rows are absent entirely: rival unit sales are not observed anywhere in the data, so only competitor price COLUMNS (our demand's response to rival prices) are identifiable.

## 3. Competitor price-follow elasticity (price_18)

Fact-table per-SKU `Competitor Price` is **100% empty**, so rival price comes from RCA `competitor_features.csv` (category x city x week median). Confounder controls identical to the production elasticity modules (cell FE, promo, OSA, month, recency x volume weights), HC1 SEs.

**Why two specs**: the paper-brief RPI form (`ln units ~ ln RPI`, RPI = own/comp price) mechanically forces comp elasticity = −own elasticity, so our own ~−1 price response leaks into it and it reads 'significant' even when rivals do nothing — we measured exactly that (median RPI coef ≈ −1). It is reported below for transparency but NEVER used. Spec B frees own and comp prices; its comp coefficient is the honest measure, gated on significance, materiality and substitutes sign (rival price up must not push our units down).

| Category | RPI coef (A) | comp elast (B) | ±SE | p (B) | used in E | verdict |
|---|---:|---:|---:|---:|---:|---|
| Besan & Gram Flour | -0.089 | -0.3389 | 0.694 | 0.6253 | 0.0 | null signal (p=0.625) |
| Dal & Pulses | -1.0726 | 1.3532 | 1.6737 | 0.4188 | 0.0 | null signal (p=0.419) |
| Honey | 2.7059 | -0.6925 | 1.3782 | 0.6154 | 0.0 | null signal (p=0.615) |
| Indian Sweets | -3.1246 | -2.6025 | 4.7407 | 0.583 | 0.0 | null signal (p=0.583) |
| Jaggery | -1.4181 | -0.2367 | 0.8566 | 0.7823 | 0.0 | null signal (p=0.782) |
| Millet & Other Atta | 1.4099 | -2.0844 | 0.8348 | 0.0125 | 0.0 | WRONG SIGN (gamma=-2.08, p=0.013) — implausible, zeroed |
| Millets | 1.3441 | -1.0335 | 1.6245 | 0.5246 | 0.0 | null signal (p=0.525) |
| Oil | -1.3768 | -6.5418 | 2.8159 | 0.0202 | 0.0 | WRONG SIGN (gamma=-6.54, p=0.020) — implausible, zeroed |
| Plain Peanuts | -0.2117 | -1.7692 | 0.6559 | 0.007 | 0.0 | WRONG SIGN (gamma=-1.77, p=0.007) — implausible, zeroed |
| Poha | 0.6274 | -0.3704 | 1.1411 | 0.7455 | 0.0 | null signal (p=0.746) |
| Rice & Rice Products | -2.0569 | -0.1882 | 2.0996 | 0.9286 | 0.0 | null signal (p=0.929) |
| Salt | -0.4573 | -2.9668 | 1.1926 | 0.0129 | 0.0 | WRONG SIGN (gamma=-2.97, p=0.013) — implausible, zeroed |
| Seeds | -1.083 | 1.4482 | 1.393 | 0.2985 | 0.0 | null signal (p=0.299) |
| Single Spice Powder | 0.5676 | 2.0084 | 2.0281 | 0.322 | 0.0 | null signal (p=0.322) |
| Sooji | 2.8572 | 0.084 | 1.3063 | 0.9487 | 0.0 | null signal (p=0.949) |
| Sugar | -1.3511 | -0.4344 | 1.6816 | 0.7962 | 0.0 | null signal (p=0.796) |
| Wheat Atta | -1.9124 | 1.3604 | 1.7554 | 0.4384 | 0.0 | null signal (p=0.438) |
| Wheat, Daliya & More | -4.7787 | 5.6174 | 3.2711 | 0.0859 | 0.0 | null signal (p=0.086) |
| Whole Spices | -1.0341 | -1.8304 | 2.3977 | 0.4452 | 0.0 | null signal (p=0.445) |

**Verdict: NULL across the board — no category shows a statistically real, material, sign-sane response to rival prices once confounders are controlled.** That is not a failure of the module; it MATCHES the challenger.py finding that competition barely moves this brand. The E matrix carries honest zeros in the competitor columns, turning a silent assumption into a measured claim.

## How to read / adopt

- `cross_price_v2.csv` = long-format E: `own_diag` rows (production own elasticities, reused untouched), `within_cat_cross` rows (decomposed), `comp_col` rows (gated). Unlisted own-brand pairs are structural zeros.
- Adoption is NOT automatic. If the verdict is ADOPT-READY, a future pricing run may point at cross_price_v2.csv via an explicit config flag — a deliberate one-line change, never done by this build.