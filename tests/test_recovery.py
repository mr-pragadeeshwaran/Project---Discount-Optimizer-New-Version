"""Ground-truth recovery: the model must recover a planted elasticity (slow)."""
import numpy as np
import pytest
from scripts.diagnostics.recovery_test import build_synthetic_panel, _naive_elasticity, _model_elasticity


def test_model_recovers_planted_elasticity_smoke():
    """Fast 1-seed guard so the core recovery property runs by default."""
    fact = build_synthetic_panel(true_elast=-1.8, seed=0)
    naive = _naive_elasticity(fact)
    model = _model_elasticity(fact)[0]
    assert abs(model - (-1.8)) <= 0.7                     # looser tol, 1 seed
    assert abs(model - (-1.8)) < abs(naive - (-1.8))      # beats the biased naive


@pytest.mark.slow
@pytest.mark.parametrize("true_e", [-1.2, -1.8])
def test_model_recovers_planted_elasticity(true_e):
    naive, cat = [], []
    for seed in range(3):
        fact = build_synthetic_panel(true_elast=true_e, seed=seed)
        naive.append(_naive_elasticity(fact))
        cat.append(_model_elasticity(fact)[0])
    naive_err = abs(np.mean(naive) - true_e)
    model_err = abs(np.mean(cat) - true_e)
    # model lands within tolerance AND clearly beats the (biased) naive fit
    assert model_err <= 0.5, f"model err {model_err:.2f} for true {true_e}"
    assert model_err < naive_err, f"model ({model_err:.2f}) not better than naive ({naive_err:.2f})"


@pytest.mark.slow
def test_naive_is_biased_more_elastic():
    fact = build_synthetic_panel(true_elast=-1.8, seed=0)
    naive = _naive_elasticity(fact)
    # the ad/discount co-timing should make naive OVER-state elasticity
    assert naive < -1.8
