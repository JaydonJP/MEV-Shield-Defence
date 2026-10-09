"""Fast ML predictor of sandwich harm (< 1 ms) trained on simulated scenarios.

The counterfactual simulator is the source of truth but costs ~100s of ms. We sample a few thousand
scenarios (trade size, liquidity, slippage, fee, gas price), label each with the exact integer AMM
sandwich model (same math the shadow fork executes), and fit a gradient-boosting regressor on log-harm.
Used for the dashboard's live mempool risk labels; decisions still use the simulator.
"""
from __future__ import annotations

import pickle
import time
from dataclasses import dataclass

import numpy as np

from . import amm_math, config

FEATURES = ["trade_usd", "liquidity_usd", "slippage", "fee_bps", "gas_price_gwei", "trade_over_liq"]


def _scenario_harm(trade_usd: float, liq_usd: float, slippage: float, fee_bps: int, gas_gwei: float) -> dict:
    """Exact sandwich outcome for one scenario (USDC-in swap on a pool with `liq_usd` USDC-side depth)."""
    E = config.E
    r_usdc = int(liq_usd * E)
    price = 2000.0
    r_weth = int(r_usdc / price)
    v = int(trade_usd * E)
    benign = amm_math.amount_out(v, r_usdc, r_weth, fee_bps)
    min_out = int(benign * (1 - slippage))
    res = amm_math.optimal_sandwich(r_usdc, r_weth, v, min_out, fee_bps)
    gas_usd = (config.SANDWICH_GAS * gas_gwei * 1e-9) * price
    profit_usd = res.profit / E * price
    harm_usd = (res.harm / E * price) if profit_usd > gas_usd else 0.0
    return {"harm_usd": harm_usd, "profit_usd": profit_usd, "attack": 1 if profit_usd > gas_usd else 0,
            "benign_out": benign}


def generate_dataset(n: int = 4000, seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X, y, cls = [], [], []
    for _ in range(n):
        trade_usd = float(10 ** rng.uniform(0.7, 4.5))
        liq_usd = float(10 ** rng.uniform(5.0, 6.7))  # $100k – $5M depth
        slippage = float(rng.choice([0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05]))
        fee_bps = int(rng.choice([5, 10, 30]))
        gas_gwei = float(rng.uniform(0.5, 5))
        r = _scenario_harm(trade_usd, liq_usd, slippage, fee_bps, gas_gwei)
        X.append([trade_usd, liq_usd, slippage, fee_bps, gas_gwei, trade_usd / liq_usd])
        y.append(r["harm_usd"])
        cls.append(r["attack"])
    return np.array(X), np.array(y), np.array(cls)


@dataclass
class PredictorMetrics:
    mae: float
    r2: float
    classifier_precision: float
    classifier_recall: float
    classifier_f1: float
    train_ms: float
    predict_us: float
    n: int


class HarmPredictor:
    def __init__(self):
        self.reg = None
        self.clf = None
        self.metrics: PredictorMetrics | None = None

    def train(self, n: int = 4000, seed: int = 0) -> PredictorMetrics:
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        from sklearn.metrics import mean_absolute_error, precision_recall_fscore_support, r2_score
        from sklearn.model_selection import train_test_split

        X, y, cls = generate_dataset(n, seed)
        ylog = np.log1p(y)
        Xtr, Xte, ytr, yte, ctr, cte = train_test_split(X, ylog, cls, test_size=0.25, random_state=seed)
        t0 = time.perf_counter()
        self.reg = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.08).fit(Xtr, ytr)
        self.clf = GradientBoostingClassifier(n_estimators=150, max_depth=3).fit(Xtr, ctr)
        train_ms = (time.perf_counter() - t0) * 1000

        pred = np.expm1(self.reg.predict(Xte))
        true = np.expm1(yte)
        cpred = self.clf.predict(Xte)
        p, r, f1, _ = precision_recall_fscore_support(cte, cpred, average="binary", zero_division=0)
        t0 = time.perf_counter()
        for _ in range(200):
            self.reg.predict(Xte[:1])
        predict_us = (time.perf_counter() - t0) / 200 * 1e6
        self.metrics = PredictorMetrics(
            mae=float(mean_absolute_error(true, pred)), r2=float(r2_score(true, pred)),
            classifier_precision=float(p), classifier_recall=float(r), classifier_f1=float(f1),
            train_ms=train_ms, predict_us=predict_us, n=n)
        return self.metrics

    def predict(self, trade_usd: float, liquidity_usd: float, slippage: float, fee_bps: int,
                gas_price_gwei: float) -> dict:
        x = np.array([[trade_usd, liquidity_usd, slippage, fee_bps, gas_price_gwei, trade_usd / liquidity_usd]])
        harm = float(max(0.0, np.expm1(self.reg.predict(x))[0]))
        p_attack = float(self.clf.predict_proba(x)[0][1])
        return {"harm_usd": harm, "p_attack": p_attack}

    def save(self, path=config.MODEL_FILE) -> None:
        with open(path, "wb") as fh:
            pickle.dump({"reg": self.reg, "clf": self.clf, "metrics": self.metrics}, fh)

    @classmethod
    def load(cls, path=config.MODEL_FILE) -> "HarmPredictor":
        inst = cls()
        with open(path, "rb") as fh:
            blob = pickle.load(fh)
        inst.reg, inst.clf, inst.metrics = blob["reg"], blob["clf"], blob["metrics"]
        return inst

    @classmethod
    def available(cls) -> bool:
        return config.MODEL_FILE.exists()
