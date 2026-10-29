"""Opt-in real EVM checks. Start `mevshield up`, then set MEVSHIELD_INTEGRATION=1."""
import os

import pytest

from mevshield import config
from mevshield.chain import Chain, ShadowFork, account
from mevshield.extensions.coordinated import analyze_sequences
from mevshield.extensions.service import isolated_world, run_feature

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("MEVSHIELD_INTEGRATION") != "1", reason="set MEVSHIELD_INTEGRATION=1 with a running devnet")]


@pytest.fixture
def world():
    chain = Chain()
    shadow = ShadowFork(chain)
    with isolated_world(chain, shadow):
        yield chain, shadow


def test_router_executes_all_legs_against_real_sandwiches(world):
    result = run_feature(*world, "routing")
    assert not result["reverted"]
    assert result["actual_out"] > 0
    assert result["attack_transactions"] >= 2
    minimum = sum(x["min_out"] for x in result["quote"]["chosen"]["legs"])
    assert result["actual_out"] >= minimum


def test_cashback_is_paid_and_all_bid_escrows_clear(world):
    chain, _ = world
    result = run_feature(*world, "cashback")
    assert result["settled"]
    assert result["rebate_usdc"] > 0
    assert result["winner"] == "relay"
    assert chain.balance("USDC", chain.deployments["contracts"]["cashback"]) == 0


def test_batch_executes_matching_and_residuals(world):
    chain, _ = world
    result = run_feature(*world, "batch")
    assert result["matched_fraction"] == pytest.approx(.75)
    assert result["alice_received_weth"] >= result["alice_amm_quote_weth"] * .98
    assert chain.balance("USDC", chain.deployments["contracts"]["batch"]) == 0
    assert chain.balance("WETH", chain.deployments["contracts"]["batch"]) == 0


def test_firewall_limits_execute_without_guardian(world):
    result = run_feature(*world, "firewall")
    assert result["borrow_succeeded"] == [True, False]
    assert result["actual_outflow_usdc"] == 8000


def test_different_sender_attack_is_confirmed_by_grouped_simulation(world):
    result = run_feature(*world, "coordinated")
    assert result["findings"]
    f = result["findings"][0]
    assert f["different_senders"] and f["explicit_link"]
    assert f["victim_harm_usd"] > 0
    assert f["actor_profit_usd"] > f["actor_gas_usd"]
    assert world[0].txpool() == []


def test_shared_pool_alone_does_not_flag_benign_swaps(world):
    chain, shadow = world
    d = chain.deployments["contracts"]
    deadline = chain.w3.eth.get_block("latest")["timestamp"] + 300
    for role in ("alice", "bob", "carol"):
        chain.send(account(role), chain.c("router").functions.swapExactIn(
            d["ammA"], d["USDC"], 100 * config.E, 0, deadline), gas=600000)
    result = analyze_sequences(chain, shadow, chain.txpool())
    assert result["edges"]
    assert result["findings"] == []
