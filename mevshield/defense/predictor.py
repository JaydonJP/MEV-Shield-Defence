"""Fast ML predictor of sandwich risk (< 1 ms), trained on simulated attack scenarios.

Why a model at all? The counterfactual shadow-fork simulator is the source of truth but costs ~100s of ms
per transaction — too slow to label every pending transaction in the mempool live. So we distil the
simulator into a model: we synthesise tens of thousands of attack scenarios (varying trade size, pool
depth, price level, slippage tolerance, fee tier and gas price), label each with the exact integer AMM
sandwich model (the same math the EVM runs), add realistic measurement noise, and fit gradient-boosted
trees. The result predicts expected sandwich harm and attack probability in under a millisecond.

This is "Flash-Loan-Aware Attack Synthesis" from the research agenda: rather than scraping a mainnet MEV
dataset (whose pools, fees and bots do not match this devnet), we generate a labelled dataset from the
attack model itself, which is both the correct training distribution for our chain and fully reproducible.
The simulator remains the source of truth for actual defense decisions; the model drives the live
dashboard's per-transaction risk labels.
"""
from __future__ import annotations

import math
import pickle
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from .. import config
from . import amm_math

# Features the model sees. The last three are engineered (computed from the raw inputs) so the trees can
# split on the quantities that actually drive sandwich economics (relative trade size, price impact).
FEATURES = ["trade_usd", "liquidity_usd", "slippage", "fee_bps", "gas_price_gwei",
            "log10_trade", "log10_liquidity", "trade_over_liquidity", "price_impact"]

PRICE_LEVELS = (1200.0, 1600.0, 2000.0, 2600.0, 3200.0)
FEE_TIERS = (5, 10, 30)
SLIPPAGES = (0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05)


def _featurize(trade_usd: float, liquidity_usd: float, slippage: float, fee_bps: float,
               gas_gwei: float) -> list[float]:
    tol = trade_usd / liquidity_usd
    # closed-form constant-product price impact of the trade alone (fraction), a strong predictor of harm
    impact = tol / (1 + tol)
    return [trade_usd, liquidity_usd, slippage, float(fee_bps), gas_gwei,
            math.log10(trade_usd), math.log10(liquidity_usd), tol, impact]


def _scenario(trade_usd: float, liq_usd: float, slippage: float, fee_bps: int, gas_gwei: float,
              price: float) -> dict:
    """Exact sandwich outcome for one scenario: a USDC-in swap on a pool with `liq_usd` of USDC-side depth
    priced at `price` USDC per WETH. Capital is bounded by a realistic inventory."""
    E = config.E
    r_usdc = int(liq_usd * E)
    r_weth = int(r_usdc / price)
    v = int(trade_usd * E)
    benign = amm_math.amount_out(v, r_usdc, r_weth, fee_bps)
    min_out = int(benign * (1 - slippage))
    capital = int(5_000_000 * E)  # attacker inventory cap (USDC)
    res = amm_math.optimal_sandwich(r_usdc, r_weth, v, min_out, fee_bps, capital=capital)
    gas_usd = config.SANDWICH_GAS * gas_gwei * 1e-9 * price
    profit_usd = res.profit / E  # USDC-denominated input token
    harm_usd = (res.harm / E * price) if profit_usd > gas_usd else 0.0
    return {"harm_usd": harm_usd, "profit_usd": profit_usd, "attack": int(profit_usd > gas_usd)}


def generate_dataset(n: int = 20_000, seed: int = 0, noise: float = 0.06):
    """Synthesise `n` labelled scenarios. Returns (X, y_harm, y_attack, feature_names)."""
    rng = np.random.default_rng(seed)
    X = np.empty((n, len(FEATURES)), dtype=float)
    y = np.empty(n, dtype=float)
    cls = np.empty(n, dtype=int)
    for i in range(n):
        trade_usd = float(10 ** rng.uniform(0.5, 4.6))  # ~$3 – $40k
        liq_usd = float(10 ** rng.uniform(4.8, 6.8))  # ~$60k – $6M depth
        slippage = float(rng.choice(SLIPPAGES))
        fee_bps = int(rng.choice(FEE_TIERS))
        gas_gwei = float(rng.uniform(0.3, 8.0))
        price = float(rng.choice(PRICE_LEVELS))
        s = _scenario(trade_usd, liq_usd, slippage, fee_bps, gas_gwei, price)
        harm = s["harm_usd"]
        if harm > 0 and noise:  # realistic multiplicative measurement noise (gas jitter, attacker competition)
            harm *= float(np.exp(rng.normal(0, noise)))
        X[i] = _featurize(trade_usd, liq_usd, slippage, fee_bps, gas_gwei)
        y[i] = harm
        cls[i] = s["attack"]
    return X, y, cls, FEATURES


@dataclass
class PredictorMetrics:
    n: int
    mae: float
    rmse: float
    r2: float
    cv_r2_mean: float
    cv_r2_std: float
    classifier_precision: float
    classifier_recall: float
    classifier_f1: float
    roc_auc: float
    attack_rate: float
    feature_importances: dict = field(default_factory=dict)
    train_ms: float = 0.0
    predict_us: float = 0.0


class HarmPredictor:
    """Two gradient-boosted models: a regressor for expected harm (USD) and a classifier for P(attack)."""

    def __init__(self):
        self.reg = None
        self.clf = None
        self.metrics: PredictorMetrics | None = None

    def train(self, n: int = 20_000, seed: int = 0, log=None) -> PredictorMetrics:
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
        from sklearn.inspection import permutation_importance
        from sklearn.metrics import (f1_score, mean_absolute_error, mean_squared_error, precision_score,
                                      r2_score, recall_score, roc_auc_score)
        from sklearn.model_selection import cross_val_score, train_test_split

        log = log or (lambda *a: None)
        X, y, cls, names = generate_dataset(n, seed)
        ylog = np.log1p(y)
        Xtr, Xte, ytr, yte, ctr, cte = train_test_split(X, ylog, cls, test_size=0.2, random_state=seed,
                                                        stratify=cls)
        log(f"  dataset: {n} scenarios, {cls.mean() * 100:.1f}% profitable-attack")

        t0 = time.perf_counter()
        self.reg = HistGradientBoostingRegressor(max_iter=500, learning_rate=0.05, max_depth=6,
                                                 l2_regularization=1.0, random_state=seed).fit(Xtr, ytr)
        self.clf = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.06, max_depth=6,
                                                  random_state=seed).fit(Xtr, ctr)
        train_ms = (time.perf_counter() - t0) * 1000

        pred = np.expm1(self.reg.predict(Xte))
        true = np.expm1(yte)
        cv = cross_val_score(self.reg, X, ylog, cv=5, scoring="r2")
        cpred = self.clf.predict(Xte)
        cprob = self.clf.predict_proba(Xte)[:, 1]
        imp = permutation_importance(self.reg, Xte, yte, n_repeats=5, random_state=seed, n_jobs=-1)
        importances = {names[i]: float(imp.importances_mean[i]) for i in np.argsort(imp.importances_mean)[::-1]}

        t0 = time.perf_counter()
        probe = Xte[:1]
        for _ in range(300):
            self.reg.predict(probe)
        predict_us = (time.perf_counter() - t0) / 300 * 1e6

        self.metrics = PredictorMetrics(
            n=n, mae=float(mean_absolute_error(true, pred)),
            rmse=float(math.sqrt(mean_squared_error(true, pred))), r2=float(r2_score(true, pred)),
            cv_r2_mean=float(cv.mean()), cv_r2_std=float(cv.std()),
            classifier_precision=float(precision_score(cte, cpred, zero_division=0)),
            classifier_recall=float(recall_score(cte, cpred, zero_division=0)),
            classifier_f1=float(f1_score(cte, cpred, zero_division=0)),
            roc_auc=float(roc_auc_score(cte, cprob)), attack_rate=float(cls.mean()),
            feature_importances=importances, train_ms=train_ms, predict_us=predict_us)
        return self.metrics

    def predict(self, trade_usd: float, liquidity_usd: float, slippage: float, fee_bps: int,
                gas_price_gwei: float) -> dict:
        x = np.array([_featurize(trade_usd, liquidity_usd, slippage, fee_bps, gas_price_gwei)])
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

    def metrics_dict(self) -> dict:
        return asdict(self.metrics) if self.metrics else {}
