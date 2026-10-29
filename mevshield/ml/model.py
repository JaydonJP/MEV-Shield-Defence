"""Dimensionless, real-market-trained V2 risk surrogate with temporal evaluation.

Real swap inputs and mathematical counterfactual labels remain separately named.
No sender, hash, realized profit, output amount or future block field is a feature.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
import pickle
import time
import tempfile

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    r2_score,
    recall_score,
    roc_auc_score,
    confusion_matrix,
)

from .. import config
from ..defense.predictor import PredictorMetrics
from .mainnet import counterfactual

FEATURES = [
    "log_trade",
    "log_liquidity",
    "slippage",
    "fee_bps",
    "log_gas_usd",
    "log_eth_price",
    "log_trade_ratio",
    "trade_ratio",
    "price_impact",
    "slippage_value_over_gas",
    "log_capital_ratio",
    "gas_over_trade",
    "slippage_over_fee",
]
INPUTS = [
    "trade_usd",
    "liquidity_usd",
    "slippage",
    "fee_bps",
    "gas_price_gwei",
    "eth_price_usd",
]
BUNDLED = Path(__file__).resolve().parent / "artifacts" / "market-risk.pkl"


def features(frame):
    a, l, s, fee, gas, eth = [np.asarray(frame[k], dtype=float) for k in INPUTS]
    if (
        not np.isfinite(np.column_stack([a, l, s, fee, gas, eth])).all()
        or np.any(a <= 0)
        or np.any(l <= 0)
        or np.any(eth <= 0)
        or np.any(gas < 0)
        or np.any((s <= 0) | (s > 0.2))
        or np.any((fee <= 0) | (fee >= 10000))
    ):
        raise ValueError("Unsupported or non-finite economic inputs")
    gas_usd = config.SANDWICH_GAS * gas * 1e-9 * eth
    ratio = a / l
    return np.column_stack(
        [
            np.log1p(a),
            np.log1p(l),
            s,
            fee,
            np.log1p(gas_usd),
            np.log(eth),
            np.log(ratio),
            ratio,
            ratio / (1 + ratio),
            np.log1p(s * a / (gas_usd + 1e-9)),
            np.log1p(5_000_000 / l),
            gas_usd / a,
            s / (fee / 10000),
        ]
    )


def temporal_split(frame):
    """Split whole blocks, chronologically, with a one-block purge at each boundary."""
    frame = frame.sort_values(["block_number", "row_id"]).reset_index(drop=True)
    if frame.row_id.duplicated().any():
        raise ValueError("Duplicate provenance IDs")
    blocks = np.sort(frame.block_number.unique())
    if len(blocks) < 100 or len(frame) < 500:
        raise ValueError("Need at least 500 rows and 100 unique blocks")
    cuts = [
        0,
        int(len(blocks) * 0.60),
        int(len(blocks) * 0.75),
        int(len(blocks) * 0.85),
        len(blocks),
    ]
    parts = []
    for i in range(4):
        chosen = blocks[cuts[i] + (1 if i else 0) : cuts[i + 1]]
        part = frame[frame.block_number.isin(chosen)].copy()
        if len(part) < 30:
            raise ValueError("Each temporal partition needs at least 30 rows")
        parts.append(part)
    return parts


def policy_scenarios(frame):
    """Declared size policies on real states, grouped with their source block.

    These are not observed trades. Untouched observed sizes receive a separate
    traffic evaluation. Four input-reserve ratios probe low and material impact.
    """
    rows = []
    for _, observed in frame.iterrows():
        rng = np.random.default_rng(
            int(hashlib.sha256(observed.row_id.encode()).hexdigest()[:16], 16)
        )
        for lower, upper in (
            (0.00001, 0.001),
            (0.001, 0.003),
            (0.003, 0.01),
            (0.01, 0.05),
        ):
            ratio = float(10 ** rng.uniform(np.log10(lower), np.log10(upper)))
            row = observed.to_dict()
            row["parent_row_id"] = row["row_id"]
            row["row_id"] += f":policy:{ratio}"
            row["trade_usd"] = row["liquidity_usd"] * ratio
            row["trade_source"] = "declared_impact_policy"
            row.update(
                counterfactual(
                    *[row[k] for k in INPUTS], attacker_gas=config.SANDWICH_GAS
                )
            )
            rows.append(row)
    return pd.DataFrame(rows)


def synthetic_lab(n=6000, seed=29):
    """Explicit lab augmentation, separate from real-input evaluation."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        row = {
            "trade_usd": float(10 ** rng.uniform(0.5, 4.8)),
            "liquidity_usd": float(10 ** rng.uniform(4.6, 7.5)),
            "slippage": float(10 ** rng.uniform(-4, -0.7)),
            "fee_bps": int(rng.choice([5, 10, 30])),
            "gas_price_gwei": float(10 ** rng.uniform(-2, 2)),
            "eth_price_usd": float(rng.uniform(1000, 6000)),
        }
        row.update(
            counterfactual(*[row[k] for k in INPUTS], attacker_gas=config.SANDWICH_GAS)
        )
        rows.append(row)
    return pd.DataFrame(rows)


def block_bootstrap(frame, harm, probability, threshold, seed=29):
    """Confidence intervals resample whole blocks, preserving correlated swaps."""
    rng = np.random.default_rng(seed)
    groups = [
        np.flatnonzero(frame.block_number.to_numpy() == n)
        for n in frame.block_number.unique()
    ]
    values = {k: [] for k in ("roc_auc", "recall", "harm_mae_usd")}
    for _ in range(150):
        idx = np.concatenate(
            [groups[i] for i in rng.integers(0, len(groups), len(groups))]
        )
        f = frame.iloc[idx]
        if f.attack.nunique() < 2:
            continue
        values["roc_auc"].append(roc_auc_score(f.attack, probability[idx]))
        values["recall"].append(
            recall_score(f.attack, probability[idx] >= threshold, zero_division=0)
        )
        values["harm_mae_usd"].append(mean_absolute_error(f.harm_usd, harm[idx]))
    return {
        key: np.quantile(v, [0.025, 0.975]).tolist() for key, v in values.items() if v
    }


def metrics(frame, harm, probability, threshold):
    y = frame.attack.to_numpy()
    true = frame.harm_usd.to_numpy()
    predicted = probability >= threshold
    return {
        "n": len(frame),
        "accuracy": float(accuracy_score(y, predicted)),
        "balanced_accuracy": (
            float(balanced_accuracy_score(y, predicted))
            if len(np.unique(y)) == 2
            else None
        ),
        "precision": (
            float(precision_score(y, predicted, zero_division=0))
            if predicted.sum()
            else None
        ),
        "recall": (
            float(recall_score(y, predicted, zero_division=0)) if y.sum() else None
        ),
        "f1": (
            float(f1_score(y, predicted, zero_division=0))
            if y.sum() or predicted.sum()
            else None
        ),
        "roc_auc": (
            float(roc_auc_score(y, probability)) if len(np.unique(y)) == 2 else None
        ),
        "pr_auc": float(average_precision_score(y, probability)) if y.sum() else None,
        "brier": float(brier_score_loss(y, probability)),
        "prevalence": float(y.mean()),
        "majority_accuracy": float(max(y.mean(), 1 - y.mean())),
        "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
        "harm_mae_usd": float(mean_absolute_error(true, harm)),
        "harm_rmse_usd": float(np.sqrt(mean_squared_error(true, harm))),
        "harm_r2": float(r2_score(true, harm)) if np.var(true) > 0 else None,
        "p95_absolute_error_usd": float(np.quantile(np.abs(true - harm), 0.95)),
    }


class MarketRiskPredictor:
    def __init__(self):
        self.reg = None
        self.clf = None
        self.probability_calibrator = None
        self.calibration = {}
        self.domain = {}
        self.report = {}
        self.metrics = None
        self.transform = "fraction"
        self.threshold = 0.5

    def _harm(self, x, frame):
        raw = self.reg.predict(x)
        harm = (
            np.expm1(raw)
            if self.transform == "dollars"
            else np.expm1(raw) / 10000 * frame.trade_usd.to_numpy()
        )
        # An economically valid output bound; not a feature derived from the target.
        cap = (
            frame.trade_usd.to_numpy()
            / (1 + frame.trade_usd.to_numpy() / frame.liquidity_usd.to_numpy())
            * frame.slippage.to_numpy()
        )
        return np.clip(harm, 0, cap)

    def _prob(self, x):
        p = np.clip(self.clf.predict_proba(x)[:, 1], 1e-7, 1 - 1e-7)
        if self.probability_calibrator is not None:
            p = self.probability_calibrator.predict_proba(
                np.log(p / (1 - p)).reshape(-1, 1)
            )[:, 1]
        return p

    def train(self, dataset, seed=29, lab_samples=6000, log=print, evaluate_test=True):
        started = time.perf_counter()
        path = Path(dataset)
        real = pd.read_csv(path)
        manifest = path.with_suffix(".manifest.json")
        if not manifest.exists():
            manifest = path.with_name("manifest.json")
        if (
            not manifest.exists()
            or json.loads(manifest.read_text())["dataset_sha256"]
            != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise ValueError("A matching dataset checksum manifest is required")
        if (
            not (real.source == "ethereum-mainnet").all()
            or not (real.label_kind == "counterfactual_v2_economics").all()
        ):
            raise ValueError(
                "Dataset provenance or label semantics do not match this model"
            )
        train, val, cal, test = temporal_split(real)
        observed_test = test.copy()
        # Split whole source blocks BEFORE creating descendants, preventing scenario leakage.
        train, val, cal, test = [
            pd.concat(
                [part.assign(trade_source="observed"), policy_scenarios(part)],
                ignore_index=True,
            )
            for part in (train, val, cal, test)
        ]
        if any(part.attack.nunique() < 2 for part in (train, val, cal, test)):
            raise ValueError("Temporal policy partitions must contain both classes")
        lab = synthetic_lab(lab_samples, seed)
        lab_train = lab.iloc[: int(0.7 * len(lab))].copy()
        lab_cal = lab.iloc[int(0.7 * len(lab)) : int(0.85 * len(lab))].copy()
        lab_test = lab.iloc[int(0.85 * len(lab)) :].copy()
        fit = pd.concat([train, lab_train], ignore_index=True)
        x = features(fit)
        xv = features(val)
        candidates = []
        # All model/threshold selection uses validation. Test rows never enter fit/calibration.
        for transform in ("fraction", "dollars"):
            target = (
                np.log1p(fit.harm_usd.to_numpy())
                if transform == "dollars"
                else np.log1p(
                    fit.harm_usd.to_numpy() / fit.trade_usd.to_numpy() * 10000
                )
            )
            for kind in ("hist", "extra"):
                reg = (
                    HistGradientBoostingRegressor(
                        max_iter=650,
                        max_leaf_nodes=31,
                        learning_rate=0.05,
                        l2_regularization=0.2,
                        early_stopping=False,
                        random_state=seed,
                    )
                    if kind == "hist"
                    else ExtraTreesRegressor(
                        n_estimators=300,
                        min_samples_leaf=2,
                        max_features=1.0,
                        n_jobs=2,
                        random_state=seed,
                    )
                )
                reg.fit(x, target)
                self.reg, self.transform = reg, transform
                h = self._harm(xv, val)
                score = float(np.mean(np.abs(np.log1p(h) - np.log1p(val.harm_usd))))
                candidates.append(
                    (
                        score,
                        reg,
                        transform,
                        {
                            "kind": kind,
                            "target": transform,
                            "validation_log_mae": score,
                        },
                    )
                )
                log(f"regressor {kind}/{transform}: validation log-MAE {score:.5f}")
        winner = min(candidates, key=lambda z: z[0])
        self.reg, self.transform = winner[1:3]
        classifiers = []
        for kind in ("hist", "extra"):
            clf = (
                HistGradientBoostingClassifier(
                    max_iter=650,
                    max_leaf_nodes=31,
                    learning_rate=0.05,
                    l2_regularization=0.2,
                    early_stopping=False,
                    random_state=seed,
                )
                if kind == "hist"
                else ExtraTreesClassifier(
                    n_estimators=300,
                    min_samples_leaf=2,
                    class_weight="balanced",
                    n_jobs=2,
                    random_state=seed,
                )
            )
            clf.fit(x, fit.attack)
            p = clf.predict_proba(xv)[:, 1]
            score = float(roc_auc_score(val.attack, p))
            classifiers.append((score, clf, kind))
            log(f"classifier {kind}: validation ROC-AUC {score:.5f}")
        chosen = max(classifiers, key=lambda z: z[0])
        self.clf = chosen[1]
        xc = features(cal)
        p = np.clip(self.clf.predict_proba(xc)[:, 1], 1e-7, 1 - 1e-7)
        self.probability_calibrator = LogisticRegression(C=10, random_state=seed).fit(
            np.log(p / (1 - p)).reshape(-1, 1), cal.attack
        )
        # Threshold picked on validation only, with balanced accuracy; no test tuning.
        pv = self._prob(xv)
        self.threshold = float(
            max(
                np.linspace(0.1, 0.9, 33),
                key=lambda t: balanced_accuracy_score(val.attack, pv >= t),
            )
        )
        for name, fitpart, calpart in (
            ("mainnet", train, cal),
            ("lab", lab_train, lab_cal),
        ):
            z = features(fitpart)
            self.domain[name] = {
                "low": z.min(axis=0),
                "high": z.max(axis=0),
                "fees": sorted(fitpart.fee_bps.unique().tolist()),
            }
            hc = self._harm(features(calpart), calpart)
            residual = np.abs(calpart.harm_usd.to_numpy() - hc) / np.maximum(1, hc)
            rank = min(len(residual), math.ceil((len(residual) + 1) * 0.9))
            self.calibration[name] = {
                "q": float(np.sort(residual)[rank - 1]),
                "n": len(residual),
                "alpha": 0.1,
            }
        self.report = {
            "schema": "market-risk-v1",
            "label_kind": "counterfactual_v2_economics",
            "promotion_gates": {
                "balanced_accuracy_min": 0.90,
                "recall_min": 0.95,
                "harm_r2_min": 0.95,
                "interval_coverage_min": 0.85,
            },
            "source": "real Ethereum states and observed swaps, plus declared impact/slippage policies and synthetic lab augmentation",
            "policy_trade_ratio_bands": [
                [0.00001, 0.001],
                [0.001, 0.003],
                [0.003, 0.01],
                [0.01, 0.05],
            ],
            "observed_input_rows": len(real),
            "slippage_sources": real.slippage_source.value_counts().to_dict(),
            "seed": seed,
            "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "features": FEATURES,
            "excluded_features": [
                "sender",
                "pool identity",
                "transaction hash",
                "actual attack outcome",
                "realized output",
            ],
            "partitions": {
                name: {
                    "n": len(part),
                    "first_block": int(part.block_number.min()),
                    "last_block": int(part.block_number.max()),
                    "observed_rows": int(part.trade_source.eq("observed").sum()),
                    "id_sha256": hashlib.sha256(
                        "\n".join(sorted(part.row_id)).encode()
                    ).hexdigest(),
                }
                for name, part in zip(
                    ("train", "validation", "calibration", "test"),
                    (train, val, cal, test),
                )
            },
            "lab_augmentation": {
                "train": len(lab_train),
                "calibration": len(lab_cal),
                "test": len(lab_test),
            },
            "candidates": [z[3] for z in candidates],
            "selected_regressor": winner[3],
            "classifier": chosen[2],
            "threshold": self.threshold,
            "evaluation_scope": "chronological held-out real contexts with observed and policy sizes; counterfactual labels, not observed attack incidence",
            "test_evaluated": False,
        }
        if evaluate_test:
            xt = features(test)
            harm = self._harm(xt, test)
            prob = self._prob(xt)
            measured = metrics(test, harm, prob, self.threshold)
            radius = self.calibration["mainnet"]["q"] * np.maximum(1, harm)
            measured["interval_coverage"] = float(
                np.mean(
                    (test.harm_usd >= np.maximum(0, harm - radius))
                    & (test.harm_usd <= harm + radius)
                )
            )
            measured["interval_mean_width_usd"] = float(
                np.mean(harm + radius - np.maximum(0, harm - radius))
            )
            measured["block_bootstrap_95ci"] = block_bootstrap(
                test, harm, prob, self.threshold, seed
            )
            self.report["test"] = measured
            self.report["test_evaluated"] = True
            self.report["observed_trade_test"] = metrics(
                observed_test,
                self._harm(features(observed_test), observed_test),
                self._prob(features(observed_test)),
                self.threshold,
            )
            self.report["observed_trade_test"]["positive_cases"] = int(
                observed_test.attack.sum()
            )
            self.report["lab_test"] = metrics(
                lab_test,
                self._harm(features(lab_test), lab_test),
                self._prob(features(lab_test)),
                self.threshold,
            )
            lab_score = self.report["lab_test"]
            self.report["lab_qualified"] = bool(
                lab_score["balanced_accuracy"] >= 0.90
                and lab_score["recall"] >= 0.95
                and lab_score["harm_r2"] >= 0.95
            )
            # Predeclared promotion gates. Falling short leaves the model advisory.
            self.report["promotion_passed"] = bool(
                measured["balanced_accuracy"] >= 0.90
                and measured["recall"] >= 0.95
                and measured["harm_r2"] >= 0.95
                and measured["interval_coverage"] >= 0.85
            )
            self.metrics = PredictorMetrics(
                n=len(real),
                mae=measured["harm_mae_usd"],
                rmse=measured["harm_rmse_usd"],
                r2=measured["harm_r2"],
                cv_r2_mean=0,
                cv_r2_std=0,
                classifier_precision=measured["precision"],
                classifier_recall=measured["recall"],
                classifier_f1=measured["f1"],
                roc_auc=measured["roc_auc"],
                attack_rate=measured["prevalence"],
                train_ms=(time.perf_counter() - started) * 1000,
                calibration_n=len(cal),
                test_n=len(test),
                interval_test_coverage=measured["interval_coverage"],
                interval_mean_width_usd=measured["interval_mean_width_usd"],
                brier_score=measured["brier"],
                evaluation_scope=self.report["evaluation_scope"],
                feature_importances={
                    name: float(value)
                    for name, value in zip(
                        FEATURES,
                        getattr(
                            self.reg, "feature_importances_", np.zeros(len(FEATURES))
                        ),
                    )
                },
            )
            self.report["by_pool"] = {
                str(pool): metrics(test.iloc[idx], harm[idx], prob[idx], self.threshold)
                for pool, idx in test.groupby("pool").indices.items()
                if test.iloc[idx].attack.nunique() == 2
            }
            # Negative control is fitted without test labels, then scored once on the same holdout.
            shuffled = HistGradientBoostingClassifier(
                max_iter=100, max_leaf_nodes=15, random_state=seed
            )
            shuffled.fit(
                features(train),
                np.random.default_rng(seed).permutation(train.attack.to_numpy()),
            )
            self.report["shuffle_control_roc_auc"] = float(
                roc_auc_score(test.attack, shuffled.predict_proba(xt)[:, 1])
            )
            self.report["latency"] = {}
            for name, function in (
                ("ml_one_row", lambda: self._harm(xt[:1], test.iloc[:1])),
                (
                    "exact_one_row",
                    lambda: counterfactual(
                        *[float(test.iloc[0][k]) for k in INPUTS],
                        attacker_gas=config.SANDWICH_GAS,
                    ),
                ),
            ):
                timings = []
                for _ in range(100):
                    begin = time.perf_counter()
                    function()
                    timings.append((time.perf_counter() - begin) * 1000)
                self.report["latency"][name + "_p50_ms"] = float(np.median(timings))
            self.metrics.predict_us = self.report["latency"]["ml_one_row_p50_ms"] * 1000
        return self.report

    def predict(
        self,
        trade_usd,
        liquidity_usd,
        slippage,
        fee_bps,
        gas_price_gwei,
        eth_price_usd=2000,
        domain="lab",
    ):
        if domain not in self.domain:
            raise ValueError("Unsupported calibration domain")
        f = pd.DataFrame(
            [
                dict(
                    zip(
                        INPUTS,
                        [
                            trade_usd,
                            liquidity_usd,
                            slippage,
                            fee_bps,
                            gas_price_gwei,
                            eth_price_usd,
                        ],
                    )
                )
            ]
        )
        x = features(f)
        harm = float(self._harm(x, f)[0])
        p = float(self._prob(x)[0])
        reasons = []
        d = self.domain[domain]
        bad = (x[0] < d["low"]) | (x[0] > d["high"])
        if bad.any() or fee_bps not in d["fees"]:
            reasons.append(
                "Outside training support: " + ", ".join(np.array(FEATURES)[bad])
            )
        radius = self.calibration[domain]["q"] * max(1, harm)
        if 2 * radius > max(25, 0.75 * harm):
            reasons.append("Harm interval is too wide")
        if abs(p - self.threshold) < 0.12:
            reasons.append("Profitability classification is uncertain")
        if domain == "mainnet" and not self.report.get("promotion_passed"):
            reasons.append("Mainnet temporal promotion gates have not passed")
        if domain == "lab" and not self.report.get("lab_qualified"):
            reasons.append("Lab qualification gates have not passed")
        return {
            "harm_usd": harm,
            "p_attack": p,
            "p_profitable": p,
            "interval_usd": [max(0, harm - radius), harm + radius],
            "nominal_coverage": 0.9,
            "accepted": not reasons,
            "status": "estimate" if not reasons else "abstained",
            "source": "market-ml",
            "reasons": reasons,
            "calibration_domain": domain,
            "scope": self.report["evaluation_scope"],
            "target": "conditional economic vulnerability, not attack occurrence",
        }

    def predict_with_fallback(self, *args, simulator=None, **kwargs):
        result = self.predict(*args, **kwargs)
        if result["accepted"]:
            return result
        if simulator is not None:
            try:
                s = simulator()
                h, p = float(s["harm_usd"]), float(s["p_attack"])
                if (
                    not math.isfinite(h)
                    or h < 0
                    or not math.isfinite(p)
                    or not 0 <= p <= 1
                ):
                    raise ValueError("Invalid simulation")
                return {
                    **result,
                    "harm_usd": h,
                    "p_attack": p,
                    "p_profitable": p,
                    "source": "simulator",
                    "status": "simulated",
                    "interval_usd": None,
                    "nominal_coverage": None,
                    "accepted": True,
                    "model_estimate": result,
                }
            except Exception:
                result["reasons"].append("Simulation failed; risk is unknown")
        return {
            **result,
            "harm_usd": None,
            "p_attack": None,
            "p_profitable": None,
            "interval_usd": None,
            "nominal_coverage": None,
            "status": "unknown",
            "model_estimate": dict(result),
        }

    def metrics_dict(self):
        return {
            **(asdict(self.metrics) if self.metrics else {}),
            "model_kind": "market-risk-v1",
            "report": self.report,
        }

    def save(self, path=config.MODEL_FILE):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as raw:
                temporary = Path(raw.name)
                with gzip.GzipFile(
                    filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
                ) as f:
                    pickle.dump({"format": "market-risk-v1", "state": self.__dict__}, f)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @classmethod
    def from_blob(cls, blob):
        m = cls()
        m.__dict__.update(blob["state"])
        return m
