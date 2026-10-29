"""Decision uncertainty, verification re-ranking and fail-closed execution boundaries."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from mevshield import config
from mevshield.defense import (
    DefenseEval,
    DefenseSelector,
    HybridPolicy,
    RiskEngine,
    RiskReport,
    Selection,
    SwapIntent,
)
from mevshield.defense.hybrid import assess_hybrid
from mevshield.defense.risk import split_amounts
from mevshield.defense.amm_math import amount_out
from mevshield.hybrid_benchmark import summarize, workload


def report(intent, harm=0, mode="hybrid", bounds=(0, 0.01)):
    return RiskReport(
        intent,
        100,
        98,
        None,
        None,
        1,
        None,
        None,
        harm,
        None,
        0,
        0.99,
        None,
        None,
        2000,
        1,
        0,
        mode,
        extra={
            "source": "market-ml" if mode == "hybrid" else "shadow-fork",
            "state": {"intent": asdict(intent)},
            "harm_interval_usd": list(bounds),
            "ml_estimate": {"harm_usd": harm},
        },
    )


class FakeEngine:
    def __init__(self, initial=None, verified_harm=0, available=True):
        self.initial = initial
        self.verified_harm = verified_harm
        self.available = available
        self.policy = HybridPolicy()
        self.stats = dict(
            ml_calls=0,
            ml_ms=0.0,
            shadow_bundles=0,
            shadow_transactions=0,
            shadow_ms=0.0,
        )
        self.stale = False

    def assess(self, intent, mode, **kwargs):
        if mode == "hybrid":
            return self.initial or report(intent)
        if not self.available:
            raise RuntimeError("Shadow unavailable")
        self.stats["shadow_bundles"] += 1
        self.stats["shadow_transactions"] += 3
        return report(intent, self.verified_harm, mode="sim")

    def assert_current(self, r, intent=None):
        if self.stale:
            raise RuntimeError("stale")
        if intent is not None and r.extra["state"]["intent"] != asdict(intent):
            raise ValueError("different intent")

    def verify_split(self, intent, n):
        self.stats["shadow_bundles"] += 1
        return self.verified_harm


def candidates(intent, mode, rep, active=False):
    return Selection(
        rep,
        [
            DefenseEval("public", rep.harm_public_usd, 0.5, 0, 0, 0),
            DefenseEval(
                "adaptive-slippage",
                0 if active else rep.harm_public_usd,
                0.5,
                0,
                0,
                0,
                params={
                    "slippage": 0.001 if active else intent.slippage,
                    "min_out": 99,
                },
            ),
            DefenseEval("private-relay", 0, 0.5, 0, 0, 0, fee=1),
            DefenseEval("commit-reveal", 0, 2, 1, 0, 0),
            DefenseEval("split", 10, 2, 1, 0, 0, params={"n": 2}),
        ],
    )


def selector(engine, monkeypatch, active=False):
    s = DefenseSelector(None, engine)
    monkeypatch.setattr(s, "_evaluate", lambda i, m, r: candidates(i, m, r, active))
    return s


def test_confident_public_uses_ml_without_simulation(monkeypatch):
    e = FakeEngine()
    s = selector(e, monkeypatch)
    result = s.evaluate(SwapIntent(10))
    assert result.best.name == "public" and result.extra["shadow_bundles"] == 0
    assert (
        result.extra["verification"] == "not-required"
        and not result.extra["risk_verified"]
    )


def test_overlapping_cost_intervals_trigger_simulation_and_reranking(monkeypatch):
    intent = SwapIntent(1000)
    e = FakeEngine(report(intent, 0.9, bounds=(0, 1.8)), verified_harm=5)
    result = selector(e, monkeypatch).evaluate(intent)
    assert result.best.name == "private-relay" and result.report.mode == "sim"
    assert result.extra["shadow_bundles"] == 1 and result.extra["fallback_reasons"]


def test_chosen_active_protection_is_verified_and_changed_cost_is_reranked(monkeypatch):
    intent = SwapIntent(1000)
    e = FakeEngine(report(intent, 5, bounds=(4.9, 5.1)), verified_harm=10)
    result = selector(e, monkeypatch, active=True).evaluate(intent)
    assert (
        result.best.name == "private-relay"
    )  # Never execute the original, now-expensive adaptive choice.
    assert result.get("adaptive-slippage").harm == 10
    assert result.extra["shadow_bundles"] == 1


def test_force_validation_and_failed_verification_never_issue_recommendation(
    monkeypatch,
):
    e = FakeEngine()
    s = selector(e, monkeypatch)
    result = s.evaluate(SwapIntent(10), require_verification=True)
    assert result.extra["risk_verified"] and result.extra["fallback_reasons"]
    e.available = False
    with pytest.raises(RuntimeError, match="unavailable"):
        s.evaluate(SwapIntent(10), require_verification=True)
    with pytest.raises(RuntimeError, match="unavailable"):
        RiskEngine(object()).assess(SwapIntent(10), mode="sim")


def test_stale_or_mismatched_decision_cannot_execute(monkeypatch):
    e = FakeEngine()
    s = selector(e, monkeypatch)
    decision = s.evaluate(SwapIntent(10))
    with pytest.raises(ValueError, match="different"):
        s.evaluate(SwapIntent(20), report=decision.report)
    e.stale = True
    monkeypatch.setattr(
        "mevshield.defense.selector.execute",
        lambda *a, **k: pytest.fail("stale decision sent a transaction"),
    )
    with pytest.raises(RuntimeError, match="stale"):
        s.execute(None, decision)


def ml_engine(estimate, amount=10):
    i = SwapIntent(amount)
    ri, ro = 200000 * config.E, 100 * config.E
    q = amount_out(int(amount * config.E), ri, ro, 5)
    context = {
        "d": {"router": "0x" + "1" * 40},
        "r_in": ri,
        "r_out": ro,
        "amount_in": int(amount * config.E),
        "fee": 5,
        "in_px": 1,
        "out_px": 2000,
        "eth_price": 2000,
        "base_fee": 0,
        "capital": 2_000_000 * config.E,
        "benign": q,
        "min_out": int(q * 0.98),
        "attacker_gas_usd": 1,
        "state": {"intent": asdict(i)},
    }
    e = FakeEngine()
    e.context = lambda intent: context
    e.predictor = SimpleNamespace(predict=lambda *a, **k: estimate)
    allowance = SimpleNamespace(call=lambda: 10**40)
    e.chain = SimpleNamespace(
        balance=lambda *a: 10**40,
        c=lambda name: SimpleNamespace(
            functions=SimpleNamespace(allowance=lambda *a: allowance)
        ),
    )
    return e, i


@pytest.mark.parametrize(
    "patch",
    [
        {"accepted": False},
        {"harm_usd": float("nan")},
        {"p_profitable": 2},
        {"interval_usd": None},
        {"interval_usd": [5, 1]},
    ],
)
def test_abstained_or_invalid_ml_output_uses_real_verification(patch):
    estimate = {
        "accepted": True,
        "harm_usd": 0.001,
        "p_profitable": 0.01,
        "interval_usd": [0, 0.1],
        **patch,
    }
    e, i = ml_engine(estimate)
    r = assess_hybrid(e, i)
    assert r.mode == "sim" and e.stats["shadow_bundles"] == 1


def test_large_trade_is_verified_even_if_model_claims_confidence():
    e, i = ml_engine(
        {"accepted": True, "harm_usd": 0, "p_profitable": 0, "interval_usd": [0, 0.01]},
        amount=25000,
    )
    r = assess_hybrid(e, i)
    assert (
        r.mode == "sim"
        and "Trade exceeds hybrid value limit" in r.extra["fallback_reasons"]
    )


def test_ml_report_does_not_fabricate_attack_trace_or_profit():
    e, i = ml_engine(
        {
            "accepted": True,
            "harm_usd": 0.001,
            "p_profitable": 0.01,
            "interval_usd": [0, 0.1],
        }
    )
    r = assess_hybrid(e, i)
    assert (
        r.mode == "hybrid"
        and r.frontrun is None
        and r.attacker_profit_usd is None
        and r.out_attacked is None
    )
    assert e.stats["shadow_bundles"] == 0


def test_split_keeps_every_input_unit_and_invalid_block_groups_fail_early():
    assert (
        split_amounts(11, 3) == [4, 4, 3]
        and sum(split_amounts(10**18 + 1, 5)) == 10**18 + 1
    )
    from mevshield.chain import ShadowFork

    with pytest.raises(ValueError, match="block groups"):
        ShadowFork.__new__(ShadowFork).simulate([], block_sizes=[1])


def test_benchmark_pairs_unique_cases_and_treats_extra_revert_as_safety_failure():
    assert len({name for name, _ in workload(False)}) == len(workload(False))
    rows = [
        {
            "case": "x",
            "repeat": 0,
            "mode": mode,
            "state_hash": "same",
            "defense": "public",
            "decision_ms": 1,
            "cpu_ms": 1,
            "shadow_bundles": 1,
            "shadow_transactions": 1,
            "harm_usd": 0,
            "realized_cost_usd": 1,
            "reverted": mode == "hybrid",
            "decision": {},
        }
        for mode in ("sim", "hybrid", "fast", "public")
    ]
    r = summarize(rows)
    assert not r["comparable_on_sample"] and r["worse_protection_cases"] == 1
    rows[-1]["state_hash"] = "different"
    with pytest.raises(ValueError, match="identical state"):
        summarize(rows)
