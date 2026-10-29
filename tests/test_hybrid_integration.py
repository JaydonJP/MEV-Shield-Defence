"""Hybrid decisions, multi-block shadow verification and state binding on real local EVMs."""

import os

import pytest

from mevshield import config
from mevshield.chain import Chain, ShadowFork, BlockBuilder
from mevshield.defense import (
    DefenseSelector,
    HarmPredictor,
    RiskEngine,
    SwapIntent,
    execute,
)
from mevshield.extensions.service import isolated_world
from mevshield.harness.sandwich_bot import SandwichBot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEVSHIELD_INTEGRATION") != "1",
        reason="requires local Anvil pair",
    ),
]


@pytest.fixture
def world():
    chain = Chain()
    shadow = ShadowFork(chain)
    with isolated_world(chain, shadow):
        yield chain, shadow


def test_confident_ml_and_forced_shadow_paths_preserve_main_state(world):
    chain, shadow = world
    selector = DefenseSelector(
        chain, RiskEngine(chain, shadow, predictor=HarmPredictor.load())
    )
    initial = (chain.block_number, chain.reserves())
    ml = selector.evaluate(SwapIntent(10))
    assert ml.extra["risk_source"] == "market-ml" and ml.extra["shadow_bundles"] == 0
    assert ml.report.attacker_profit_usd is None
    verified = selector.evaluate(SwapIntent(10), require_verification=True)
    assert verified.extra["risk_verified"] and verified.extra["shadow_bundles"] > 0
    assert (chain.block_number, chain.reserves()) == initial
    chain.mine()
    with pytest.raises(RuntimeError, match="stale"):
        selector.execute(BlockBuilder(chain), ml)
    assert not chain.txpool()


def test_hybrid_selected_active_protection_executes_without_loss(world):
    chain, shadow = world
    engine = RiskEngine(chain, shadow, predictor=HarmPredictor.load())
    selector = DefenseSelector(chain, engine)
    selection = selector.evaluate(SwapIntent(1000))
    assert selection.extra["shadow_bundles"] > 0
    if selection.best.name == "adaptive-slippage":
        assert selection.best.params["verified"]
    result = selector.execute(
        BlockBuilder(chain, [SandwichBot(chain, shadow)]),
        selection,
        log=lambda *a: None,
    )
    assert not result.reverted and result.harm_usd < 1


def test_split_sequence_verification_matches_actual_attacked_execution(world):
    chain, shadow = world
    intent = SwapIntent(1000.000000000001)
    engine = RiskEngine(chain, shadow)
    initial = (chain.block_number, chain.reserves())
    predicted = engine.verify_split(intent, 3)
    assert (chain.block_number, chain.reserves()) == initial
    assert engine.stats["shadow_bundles"] == 1
    result = execute(
        chain,
        BlockBuilder(chain, [SandwichBot(chain, shadow)]),
        intent,
        "split",
        {"n": 3},
        log=lambda *a: None,
    )
    assert not result.reverted and abs(result.harm_usd - predicted) < 0.02
