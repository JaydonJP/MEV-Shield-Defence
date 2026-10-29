import pickle

import numpy as np
import pytest

from mevshield.defense.predictor import HarmPredictor, generate_dataset


@pytest.fixture(scope="module")
def model():
    m = HarmPredictor()
    m.train(n=3000, seed=73)
    return m


def test_calibration_and_test_sets_are_separate_and_measured(model):
    assert model.metrics.calibration_n == model.metrics.test_n == 600
    assert 0.80 <= model.metrics.interval_test_coverage <= 1
    assert 0 <= model.metrics.brier_score < .15
    assert "synthetic" in model.metrics.evaluation_scope


def test_new_independent_synthetic_seed_interval_coverage(model):
    x, y, _, _ = generate_dataset(300, seed=893)
    pred = np.maximum(0, np.expm1(model.reg.predict(x)))
    radius = model.calibration["q"] * np.maximum(1, pred)
    assert np.mean((y >= np.maximum(0, pred - radius)) & (y <= pred + radius)) >= .78


def test_unseen_fee_and_extreme_liquidity_abstain(model):
    result = model.predict(1000, 1000, .02, 100, 1)
    assert not result["accepted"] and result["status"] == "abstained"
    assert any("Outside training" in r for r in result["reasons"])


def test_simulator_fallback_and_failure_do_not_invent_safe_label(model):
    params = (1000, 1000, .02, 100, 1)
    result = model.predict_with_fallback(*params, simulator=lambda: {"harm_usd": 12.5, "p_attack": 1})
    assert result["source"] == "simulator" and result["harm_usd"] == 12.5 and result["accepted"]
    assert result["interval_usd"] is None
    unknown = model.predict_with_fallback(*params, simulator=lambda: {"harm_usd": float("nan"), "p_attack": 0})
    assert unknown["status"] == "unknown" and unknown["harm_usd"] is None and unknown["p_attack"] is None
    assert not unknown["accepted"]


def test_calibration_round_trip_and_legacy_artifact(tmp_path, model):
    path = tmp_path / "model.pkl"
    model.save(path)
    restored = HarmPredictor.load(path)
    assert restored.predict(1000, 200000, .02, 5, 1) == model.predict(1000, 200000, .02, 5, 1)
    with path.open("wb") as f:
        pickle.dump({"reg": model.reg, "clf": model.clf, "metrics": model.metrics}, f)
    legacy = HarmPredictor.load(path)
    assert legacy.predict_with_fallback(1000, 200000, .02, 5, 1)["status"] == "unknown"


@pytest.mark.parametrize("params", [(0, 1, .02, 5, 1), (1, 0, .02, 5, 1),
    (1, 1, float("nan"), 5, 1), (1, 1, .02, 10000, 1)])
def test_invalid_inputs_rejected(model, params):
    with pytest.raises(ValueError): model.predict(*params)
