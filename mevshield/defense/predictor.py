"""Legacy synthetic predictor and compatible loader for the real-context model.

The current real-context training/evaluation pipeline lives in mevshield.ml.
The original lab-only implementation remains available with train --synthetic.

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
    calibration_n: int = 0
    test_n: int = 0
    interval_nominal_coverage: float = 0.9
    interval_test_coverage: float = 0.0
    interval_mean_width_usd: float = 0.0
    brier_score: float = 0.0
    evaluation_scope: str = "held-out synthetic AMM scenarios; not historical/mainnet validation"


class HarmPredictor:
    """Two gradient-boosted models: a regressor for expected harm (USD) and a classifier for P(attack)."""

    def __init__(self):
        self.reg = None
        self.clf = None
        self.metrics: PredictorMetrics | None = None
        self.calibration = None
        self.probability_calibrator = None
        self.domain = None

    def train(self, n: int = 20_000, seed: int = 0, log=None) -> PredictorMetrics:
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
        from sklearn.inspection import permutation_importance
        from sklearn.metrics import (f1_score, mean_absolute_error, mean_squared_error, precision_score,
                                      r2_score, recall_score, roc_auc_score)
        from sklearn.model_selection import cross_val_score, train_test_split

        log = log or (lambda *a: None)
        X, y, cls, names = generate_dataset(n, seed)
        ylog = np.log1p(y)
        if n < 500:
            raise ValueError("Use at least 500 samples for separate training, calibration and test sets")
        Xfit, Xte, yfit, yte, cfit, cte = train_test_split(
            X, ylog, cls, test_size=0.2, random_state=seed, stratify=cls)
        Xtr, Xcal, ytr, ycal, ctr, ccal = train_test_split(
            Xfit, yfit, cfit, test_size=0.25, random_state=seed + 1, stratify=cfit)
        log(f"  dataset: {n} scenarios, {cls.mean() * 100:.1f}% profitable-attack")

        t0 = time.perf_counter()
        self.reg = HistGradientBoostingRegressor(max_iter=500, learning_rate=0.05, max_depth=6,
                                                 l2_regularization=1.0, random_state=seed).fit(Xtr, ytr)
        self.clf = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.06, max_depth=6,
                                                  random_state=seed).fit(Xtr, ctr)
        # Separate calibration set: never used to fit the base models.
        from sklearn.linear_model import LogisticRegression
        cal_prob = np.clip(self.clf.predict_proba(Xcal)[:, 1], 1e-6, 1 - 1e-6)
        self.probability_calibrator = LogisticRegression(random_state=seed).fit(
            np.log(cal_prob / (1 - cal_prob)).reshape(-1, 1), ccal)
        cal_harm = np.maximum(0, np.expm1(self.reg.predict(Xcal)))
        residuals = np.abs(np.expm1(ycal) - cal_harm) / np.maximum(1.0, cal_harm)
        rank = min(len(residuals), math.ceil((len(residuals) + 1) * 0.9))
        self.calibration = {"q": float(np.sort(residuals)[rank - 1]), "alpha": 0.1,
                            "n": len(residuals), "scope": "synthetic exchangeable AMM scenarios"}
        self.domain = {"low": Xtr.min(axis=0).tolist(), "high": Xtr.max(axis=0).tolist(),
                       "fee_tiers": sorted(set(Xtr[:, 3].tolist()))}
        train_ms = (time.perf_counter() - t0) * 1000

        pred = np.expm1(self.reg.predict(Xte))
        true = np.expm1(yte)
        cv = cross_val_score(self.reg, Xtr, ytr, cv=5, scoring="r2")
        cprob = self._probabilities(Xte)
        cpred = (cprob >= 0.5).astype(int)
        imp = permutation_importance(self.reg, Xte, yte, n_repeats=5, random_state=seed, n_jobs=1)
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
        radius = self.calibration["q"] * np.maximum(1.0, pred)
        self.metrics.calibration_n = len(Xcal)
        self.metrics.test_n = len(Xte)
        self.metrics.interval_test_coverage = float(np.mean(
            (true >= np.maximum(0, pred - radius)) & (true <= pred + radius)))
        self.metrics.interval_mean_width_usd = float(np.mean(pred + radius - np.maximum(0, pred - radius)))
        self.metrics.brier_score = float(np.mean((cprob - cte) ** 2))
        return self.metrics

    def _probabilities(self, x):
        p = np.clip(self.clf.predict_proba(x)[:, 1], 1e-6, 1 - 1e-6)
        if self.probability_calibrator is not None:
            return self.probability_calibrator.predict_proba(np.log(p / (1 - p)).reshape(-1, 1))[:, 1]
        return p

    def predict(self, trade_usd: float, liquidity_usd: float, slippage: float, fee_bps: int,
                gas_price_gwei: float) -> dict:
        values = (trade_usd, liquidity_usd, slippage, fee_bps, gas_price_gwei)
        if not all(math.isfinite(v) for v in values) or min(trade_usd, liquidity_usd) <= 0 \
                or not 0 < slippage < 1 or not 0 <= fee_bps < 10000 or gas_price_gwei < 0:
            raise ValueError("Invalid risk inputs")
        if self.reg is None or self.clf is None:
            raise ValueError("Risk model has not been trained")
        x = np.array([_featurize(trade_usd, liquidity_usd, slippage, fee_bps, gas_price_gwei)])
        harm = float(max(0.0, np.expm1(self.reg.predict(x))[0]))
        p_attack = float(self._probabilities(x)[0])
        reasons = []
        interval = None
        if not self.calibration or not self.domain:
            reasons.append("Model artifact has no calibration; retrain it")
        else:
            low, high = np.array(self.domain["low"]), np.array(self.domain["high"])
            outside = [FEATURES[i] for i in np.flatnonzero((x[0] < low) | (x[0] > high))]
            if outside or fee_bps not in self.domain["fee_tiers"]:
                reasons.append("Outside training support: " + ", ".join(outside or ["fee tier"]))
            radius = self.calibration["q"] * max(1.0, harm)
            interval = [max(0.0, harm - radius), harm + radius]
            if radius * 2 > max(25.0, harm * 0.75):
                reasons.append("Harm interval is too wide for an ML-only label")
            if 0.35 <= p_attack <= 0.65:
                reasons.append("Attack probability is near the decision boundary")
        return {"harm_usd": harm, "p_attack": p_attack, "interval_usd": interval,
                "nominal_coverage": 0.9 if interval else None, "accepted": not reasons,
                "status": "estimate" if not reasons else "abstained", "source": "ml",
                "reasons": reasons, "scope": "synthetic local AMM model; coverage may fail under drift"}

    def predict_with_fallback(self, *args, simulator=None) -> dict:
        result = self.predict(*args)
        if result["accepted"]:
            return result
        if simulator is not None:
            try:
                sim = simulator()
                harm, probability = float(sim["harm_usd"]), float(sim["p_attack"])
                if not math.isfinite(harm) or harm < 0 or not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError("Invalid simulator output")
                return {**result, "source": "simulator", "status": "simulated", "harm_usd": harm,
                        "p_attack": probability, "interval_usd": None, "nominal_coverage": None,
                        "model_estimate": result, "accepted": True}
            except Exception:
                result["reasons"].append("Simulator unavailable or failed; risk remains unknown")
        return {**result, "status": "unknown", "harm_usd": None, "p_attack": None,
                "interval_usd": None, "nominal_coverage": None, "model_estimate": dict(result)}

    def save(self, path=config.MODEL_FILE) -> None:
        with open(path, "wb") as fh:
            pickle.dump({"reg": self.reg, "clf": self.clf, "metrics": self.metrics,
                         "calibration": self.calibration, "domain": self.domain,
                         "probability_calibrator": self.probability_calibrator}, fh)

    @classmethod
    def load(cls, path=config.MODEL_FILE) -> "HarmPredictor":
        inst = cls()
        from pathlib import Path
        from ..ml.model import BUNDLED
        if path == config.MODEL_FILE and BUNDLED.exists():
            if not Path(path).exists() or BUNDLED.stat().st_mtime > Path(path).stat().st_mtime:
                path = BUNDLED
        if Path(path).resolve() == BUNDLED.resolve():
            import hashlib, json
            card = json.loads(BUNDLED.with_suffix(".json").read_text())
            if hashlib.sha256(BUNDLED.read_bytes()).hexdigest() != card["artifact_sha256"]:
                raise ValueError("Bundled model checksum mismatch; restore the trusted artifact or retrain")
        import gzip
        with open(path, "rb") as header:
            compressed = header.read(2) == b"\x1f\x8b"
        with (gzip.open(path, "rb") if compressed else open(path, "rb")) as fh:
            blob = pickle.load(fh)
        if blob.get("format") == "market-risk-v1":
            from ..ml.model import MarketRiskPredictor
            return MarketRiskPredictor.from_blob(blob)
        inst.reg, inst.clf, inst.metrics = blob["reg"], blob["clf"], blob["metrics"]
        inst.calibration = blob.get("calibration")
        inst.domain = blob.get("domain")
        inst.probability_calibrator = blob.get("probability_calibrator")
        return inst

    @classmethod
    def available(cls) -> bool:
        from ..ml.model import BUNDLED
        return config.MODEL_FILE.exists() or BUNDLED.exists()

    def metrics_dict(self) -> dict:
        return asdict(self.metrics) if self.metrics else {}
