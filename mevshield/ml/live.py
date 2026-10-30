"""Read-only mainnet economic assessment for explicitly supported V2 pairs."""

import math
import time

from web3 import Web3

from .. import config
from .mainnet import DEFAULT_RPC, POOLS, TOKENS, ReadOnlyRPC, counterfactual
from .model import MarketRiskPredictor


def _assessment(inputs, model):
    """Keep displayed economics consistent across the ML support boundary.

    Regression output is advisory. In particular, a tiny positive ML residual
    must not imply a profitable attack when modeled proceeds cannot pay gas.
    """
    exact = counterfactual(*inputs, attacker_gas=config.SANDWICH_GAS)
    estimate = None
    reasons = ["Headline harm uses explicit V2 economics; ML is shown separately as advisory evidence"]
    if isinstance(model, MarketRiskPredictor):
        try:
            estimate = model.predict(*inputs, domain="mainnet")
        except Exception:
            reasons.append("ML inference unavailable; the V2 calculation is unaffected")
    if not exact["attack"]:
        reasons.append("No profitable completed sandwich found after attacker gas under these assumptions; this is not a safe-transaction verdict")
    return {
        "source": "exact-v2", "status": "calculated", "accepted": True,
        "harm_usd": exact["harm_usd"], "p_attack": exact["attack"],
        "p_profitable": exact["attack"], "interval_usd": None,
        "nominal_coverage": None, "model_estimate": estimate,
        "economics": {**exact, "net_profit_usd": exact["gross_profit_usd"] - exact["attacker_gas_usd"]},
        "reasons": reasons,
    }


def assess_mainnet(pool, amount, slippage=0.01, rpc=DEFAULT_RPC, model=None):
    pool = pool.lower()
    if pool not in POOLS:
        raise ValueError(
            "Unsupported pool; only the documented V2 allowlist is supported"
        )
    if (
        not math.isfinite(amount)
        or not 0 < amount <= 5_000_000
        or not math.isfinite(slippage)
        or not 0 < slippage <= 0.2
    ):
        raise ValueError("Invalid amount or slippage")
    client = ReadOnlyRPC(rpc)
    if int(client.call("eth_chainId", []), 16) != 1:
        raise ValueError("Mainnet RPC chain mismatch")
    block = client.call("eth_getBlockByNumber", ["latest", False])
    tag = block["number"]
    if not -60 <= time.time() - int(block["timestamp"], 16) <= 300:
        raise ValueError(
            "RPC returned a stale or future block; no current risk assessment is available"
        )
    calls = [
        ("eth_call", [{"to": pool, "data": selector}, tag])
        for selector in ("0x0dfe1681", "0xd21220a7", "0x0902f1ac", "0xc45a0155")
    ]
    token0, token1, reserves, factory = client.batch(calls)
    metadata = POOLS[pool]
    stable = TOKENS[metadata["stable"]]
    tokens = ["0x" + token0[-40:].lower(), "0x" + token1[-40:].lower()]
    expected = (
        [stable, TOKENS["WETH"]] if metadata["stable0"] else [TOKENS["WETH"], stable]
    )
    expected_factory = (
        "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"
        if "Sushi" in metadata["name"]
        else "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
    )
    if tokens != expected or ("0x" + factory[-40:]).lower() != expected_factory:
        raise ValueError("Pool identity, factory or token ordering mismatch")
    raw = bytes.fromhex(reserves[2:])
    r0, r1 = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big")
    stable_r, eth_r = (r0, r1) if metadata["stable0"] else (r1, r0)
    if min(stable_r, eth_r) <= 0:
        raise ValueError("Pool has no liquidity")
    liquidity = stable_r / 10 ** metadata["decimals"]
    eth_price = liquidity / (eth_r / 10**18)
    gas = int(client.call("eth_gasPrice", []), 16) / 1e9
    inputs = (amount, liquidity, slippage, 30, gas, eth_price)
    result = _assessment(inputs, model)
    # One reserve/block read and one gas quote for every comparison row. Separate
    # live requests can see different markets and are not a controlled comparison.
    comparison = []
    for tolerance in sorted({slippage, .04, .10}):
        row = result if tolerance == slippage else _assessment(
            (amount, liquidity, tolerance, 30, gas, eth_price), model)
        comparison.append({"slippage": tolerance, **row})
    return {
        **result,
        "comparison": comparison,
        "comparison_scope": "same block reserves, trade, gas quote, ETH price and attacker assumptions; only slippage changes",
        "chain_id": 1,
        "block": int(tag, 16),
        "block_hash": block["hash"],
        "pool": pool,
        "pool_name": metadata["name"],
        "token_in": metadata["stable"],
        "token_out": "WETH",
        "inputs": dict(
            zip(
                (
                    "trade_usd",
                    "liquidity_usd",
                    "slippage",
                    "fee_bps",
                    "gas_price_gwei",
                    "eth_price_usd",
                ),
                inputs,
            )
        ),
        "scope": "read-only V2 what-if; not transaction signing, observed attack probability, or guaranteed execution",
        "assumptions": {
            "attacker_capital_usd": 5_000_000,
            "attacker_gas_units": config.SANDWICH_GAS,
            "stablecoin_usd": 1,
            "ordering": "front/victim/back",
            "reference_price": "pool spot, not an independent oracle",
        },
    }
