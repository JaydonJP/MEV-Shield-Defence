"""Unit tests for the ML fast predictor (no devnet needed)."""
from mevshield.defense.predictor import HarmPredictor, generate_dataset


def test_dataset_shapes():
    X, y, cls, names = generate_dataset(400, seed=0)
    assert X.shape == (400, len(names))
    assert len(y) == 400 and len(cls) == 400
    assert set(cls.tolist()) <= {0, 1}


def test_predictor_trains_and_is_accurate():
    m = HarmPredictor()
    metrics = m.train(n=4000, seed=0)
    assert metrics.r2 > 0.85
    assert metrics.cv_r2_mean > 0.80
    assert metrics.roc_auc > 0.95
    assert metrics.classifier_f1 > 0.9
    assert metrics.predict_us < 20_000  # well under 20 ms
    assert metrics.feature_importances  # non-empty


def test_predictor_monotone_in_trade_size():
    m = HarmPredictor()
    m.train(n=1500, seed=0)
    small = m.predict(10, 200_000, 0.02, 5, 1.0)
    big = m.predict(10_000, 200_000, 0.02, 5, 1.0)
    assert big["harm_usd"] > small["harm_usd"]
    assert big["p_attack"] >= small["p_attack"]
