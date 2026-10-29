"""Selective ML prediction with explicit, fail-closed shadow verification rules."""

from dataclasses import dataclass
import math
import time

from .. import config
from ..chain import account
from . import amm_math
from .risk import RiskReport


@dataclass(frozen=True)
class HybridPolicy:
    # Fixed before the decision benchmark; not fitted to its outcomes.
    cost_margin_usd: float = 0.10
    max_trade_usd: float = 25_000
    max_price_impact_ratio: float = 0.10
    max_harm_upper_usd: float = 250

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in self.__dict__.values()):
            raise ValueError("Hybrid policy limits must be finite and positive")


def policy_for(engine):
    return engine.policy or HybridPolicy()


def verified_report(engine, intent, reasons, estimate=None, min_out=None):
    # Never silently substitute exact arithmetic if the required EVM is unavailable.
    report = engine.assess(intent, mode="sim", leakage_levels=False, min_out=min_out)
    report.extra.update(fallback_reasons=list(reasons), ml_estimate=estimate)
    return report


def assess_hybrid(engine, intent, min_out=None):
    started = time.perf_counter()
    ctx = engine.context(intent)
    reasons = []
    estimate = None
    model = engine.predictor
    try:
        if model is None:
            from .predictor import HarmPredictor

            if not HarmPredictor.available():
                raise ValueError("ML artifact unavailable")
            model = engine.predictor = HarmPredictor.load()
        prediction_started = time.perf_counter()
        engine.stats["ml_calls"] += 1
        try:
            estimate = model.predict(
                intent.amount * ctx["in_px"],
                ctx["r_in"] / config.E * ctx["in_px"],
                intent.slippage,
                ctx["fee"],
                (ctx["base_fee"] + intent.tip + config.GWEI) / config.GWEI,
                eth_price_usd=ctx["eth_price"],
                domain="lab",
            )
        finally:
            engine.stats["ml_ms"] += (time.perf_counter() - prediction_started) * 1000
        if not estimate.get("accepted"):
            reasons.extend(estimate.get("reasons") or ["ML abstained"])
        h = estimate.get("harm_usd")
        p = estimate.get("p_profitable", estimate.get("p_attack"))
        bounds = estimate.get("interval_usd")
        if (
            h is None
            or p is None
            or not math.isfinite(h)
            or h < 0
            or not math.isfinite(p)
            or not 0 <= p <= 1
        ):
            reasons.append("Invalid ML harm or profitability score")
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 2
            or not all(math.isfinite(v) for v in bounds)
            or h is None
            or not 0 <= bounds[0] <= h <= bounds[1]
        ):
            reasons.append("Missing or invalid ML harm interval")
    except Exception as exc:
        reasons.append(f"ML unavailable: {type(exc).__name__}")
        if not isinstance(estimate, dict):
            estimate = None
    policy = policy_for(engine)
    if intent.amount * ctx["in_px"] >= policy.max_trade_usd:
        reasons.append("Trade exceeds hybrid value limit")
    if ctx["amount_in"] / ctx["r_in"] >= policy.max_price_impact_ratio:
        reasons.append("Trade exceeds hybrid impact limit")
    if (
        estimate
        and estimate.get("interval_usd")
        and isinstance(estimate["interval_usd"], (list, tuple))
    ):
        if (
            len(estimate["interval_usd"]) == 2
            and isinstance(estimate["interval_usd"][1], (int, float))
            and estimate["interval_usd"][1] >= policy.max_harm_upper_usd
        ):
            reasons.append("Potential harm requires additional verification")
    if min_out is not None:
        reasons.append("Custom output bound requires verification")
    # The model assumes up to $5m. Accept it only where the slippage-constrained
    # attack fits both the model cap and the actual local attacker's inventory.
    bound = amm_math.max_frontrun_without_revert(
        ctx["r_in"], ctx["r_out"], ctx["amount_in"], ctx["min_out"], ctx["fee"]
    )
    if bound > min(ctx["capital"], int(5_000_000 / ctx["in_px"] * config.E)):
        reasons.append("Attacker capital differs from model assumptions")
    user = account(intent.role).address
    if (
        engine.chain.balance(intent.token_in, user) < ctx["amount_in"]
        or engine.chain.c(intent.token_in)
        .functions.allowance(user, ctx["d"]["router"])
        .call()
        < ctx["amount_in"]
    ):
        reasons.append("Balance or allowance needs execution validation")
    if reasons:
        return verified_report(engine, intent, reasons, estimate, min_out)
    report = RiskReport(
        intent=intent,
        out_benign=ctx["benign"],
        min_out=ctx["min_out"],
        frontrun=None,
        attacker_profit_usd=None,
        attacker_gas_usd=ctx["attacker_gas_usd"],
        attack_profitable=None,
        out_attacked=None,
        harm_public_usd=float(h),
        harm_hint_usd=None,
        harm_private_usd=0,
        p_attack=float(p),
        closed_form_harm_usd=None,
        closed_form_error_pct=None,
        out_price_usd=ctx["out_px"],
        in_price_usd=ctx["in_px"],
        sim_ms=(time.perf_counter() - started) * 1000,
        mode="hybrid",
        evals=0,
        extra={
            "ctx_fee": ctx["fee"],
            "r_in": ctx["r_in"],
            "r_out": ctx["r_out"],
            "amount_in": ctx["amount_in"],
            "capital": ctx["capital"],
            "state": ctx["state"],
            "source": "market-ml",
            "ml_estimate": estimate,
            "fallback_reasons": [],
            "harm_interval_usd": list(bounds),
        },
    )
    engine.assert_current(report)
    return report
