"""Unit tests for the ML fast predictor (no devnet needed)."""
from mevshield.predictor import HarmPredictor, generate_dataset


def test_dataset_shapes():
    X, y, cls = generate_dataset(200, seed=0)
    assert X.shape == (200, 6)
    assert len(y) == 200 and len(cls) == 200
    assert set(cls.tolist()) <= {0, 1}


def test_predictor_trains_and_is_accurate():
    m = HarmPredictor()
    metrics = m.train(n=1500, seed=0)
    assert metrics.r2 > 0.8
    assert metrics.classifier_f1 > 0.9
    assert metrics.predict_us < 20_000  # well under 20 ms


def test_predictor_monotone_in_trade_size():
    m = HarmPredictor()
    m.train(n=1500, seed=0)
    small = m.predict(10, 200_000, 0.02, 5, 1.0)
    big = m.predict(10_000, 200_000, 0.02, 5, 1.0)
    assert big["harm_usd"] > small["harm_usd"]
    assert big["p_attack"] >= small["p_attack"]
