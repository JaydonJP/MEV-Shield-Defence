"""MEV-adjusted direct-pool allocation. Amounts stay integer-valued through quote and execution."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .. import config
from ..chain import BlockBuilder, Chain, account, fn_simtx
from ..defense import amm_math


@dataclass(frozen=True)
class PoolView:
    name: str
    weth: int
    usdc: int
    fee_bps: int = 5


def quote_routes(amount: int, pools: list[PoolView], *, token_in: str = "USDC", slippage: float = .02,
                 gas_per_leg_usd: float = 1, attacker_gas_usd: float = 2,
                 capital: int = 2_000_000 * config.E, reference_price: float = 2000) -> dict:
    """Five allocations across exactly two venues. Independent per-leg sandwich model, public orderflow.

    Each leg's minimum is enforced on-chain. This is a model estimate, not a universal loss bound.
    Pool fees already enter the amount-out formula and must not be subtracted a second time.
    """
    if (amount <= 0 or len(pools) != 2 or token_in not in ("USDC", "WETH") or
            not math.isfinite(slippage) or not 0 <= slippage < 1 or reference_price <= 0 or
            not all(p.weth > 0 and p.usdc > 0 and 0 <= p.fee_bps < 10_000 for p in pools) or
            min(gas_per_leg_usd, attacker_gas_usd, capital) < 0):
        raise ValueError("invalid route inputs")
    if pools[0].name == pools[1].name:
        raise ValueError("venues must be distinct")
    in_px = 1 if token_in == "USDC" else reference_price
    out_px = reference_price if token_in == "USDC" else 1
    candidates = []
    for share in (0, 25, 50, 75, 100):
        amounts = [amount * share // 100, amount - amount * share // 100]
        legs = []
        for pool, size in zip(pools, amounts):
            if not size:
                continue
            ri, ro = (pool.usdc, pool.weth) if token_in == "USDC" else (pool.weth, pool.usdc)
            quoted = amm_math.amount_out(size, ri, ro, pool.fee_bps)
            minimum = int(quoted * (1 - slippage))
            attack = amm_math.optimal_sandwich(ri, ro, size, minimum, pool.fee_bps, capital=capital)
            profitable = attack.profit / config.E * in_px > attacker_gas_usd
            harm = attack.harm if profitable else 0
            legs.append({"pool": pool.name, "amount_in": size, "quoted_out": quoted, "min_out": minimum,
                         "predicted_harm": harm, "frontrun": attack.frontrun if profitable else 0})
        quoted = sum(x["quoted_out"] for x in legs)
        harm = sum(x["predicted_harm"] for x in legs)
        gas = gas_per_leg_usd * len(legs)
        candidates.append({"share_a_pct": share, "legs": legs, "quoted_out": quoted,
                           "predicted_harm_usd": harm / config.E * out_px, "gas_usd": gas,
                           "expected_net_usd": (quoted - harm) / config.E * out_px - gas})
    chosen = max(candidates, key=lambda r: r["expected_net_usd"])
    best_quote = max(candidates, key=lambda r: r["quoted_out"])
    return {"token_in": token_in, "amount_in": amount, "reference_price": reference_price,
            "assumption": "public orderflow; independent rational sandwiches; per-leg limits; estimated gas",
            "pools": [asdict(p) for p in pools], "candidates": candidates, "chosen": chosen,
            "best_quote": best_quote,
            "modeled_improvement_usd": chosen["expected_net_usd"] - best_quote["expected_net_usd"]}


def chain_quote(chain: Chain, amount: float, token_in: str = "USDC", slippage: float = .02) -> dict:
    if not math.isfinite(amount) or not 0 < amount <= 1_000_000:
        raise ValueError("amount must be finite and in (0, 1000000]")
    pools = [PoolView(key, *chain.reserves(key), chain.c(key).functions.feeBps().call())
             for key in ("ammA", "ammB")]
    return quote_routes(int(amount * config.E), pools, token_in=token_in, slippage=slippage,
                        gas_per_leg_usd=chain.gas_cost_usd(config.SWAP_GAS),
                        attacker_gas_usd=chain.gas_cost_usd(config.SANDWICH_GAS, 2 * config.GWEI),
                        reference_price=chain.eth_price())


def execute_route(chain: Chain, shadow, amount: float, token_in: str = "USDC", slippage: float = .02,
                  attack: bool = True) -> dict:
    report = chain_quote(chain, amount, token_in, slippage)
    route = report["chosen"]
    d = chain.deployments["contracts"]
    out = "WETH" if token_in == "USDC" else "USDC"
    legs = route["legs"]
    deadline = chain.deadline()
    fn = chain.c("mevRouter").functions.swap(
        [d[x["pool"]] for x in legs], [x["amount_in"] for x in legs], [x["min_out"] for x in legs],
        d[token_in], d[out], sum(x["min_out"] for x in legs), deadline)
    sw = chain.c("sandwich")
    bot = account("sandwich_bot")
    attacked = [x for x in legs if x["frontrun"] > 0] if attack else []
    fronts = [sw.functions.frontrun(d[x["pool"]], d[token_in], x["frontrun"], 0) for x in attacked]
    backs = [sw.functions.backrun(d[x["pool"]], d[out], 0) for x in attacked]
    shadow.sync()
    bundle = ([fn_simtx(bot.address, f) for f in fronts] + [fn_simtx(account("alice").address, fn)] +
              [fn_simtx(bot.address, f) for f in backs])
    probe = shadow.simulate(bundle)
    if not all(r["status"] == 1 for r in probe.receipts):
        raise RuntimeError("route bundle failed preflight; refresh quote")
    before = chain.balance(out, account("alice").address)
    hashes = [chain.send(bot, f, tip=2 * config.GWEI, gas=600_000) for f in fronts]
    h = chain.send(account("alice"), fn, tip=config.GWEI, gas=1_500_000)
    hashes.append(h)
    hashes.extend(chain.send(bot, f, tip=config.GWEI // 2, gas=600_000) for f in backs)
    block = BlockBuilder(chain).build_block()
    receipts = [chain.receipt(x) for x in hashes]
    actual = chain.balance(out, account("alice").address) - before
    gas_wei = sum(r["gasUsed"] * r["effectiveGasPrice"] for r in receipts if r["transactionHash"].to_0x_hex() == h)
    return {"quote": report, "tx_hash": h, "block": block.number, "actual_out": actual,
            "token_out": out, "reverted": chain.receipt(h)["status"] != 1,
            "attack_transactions": len(fronts) + len(backs), "gas_usd": gas_wei / config.E * report["reference_price"],
            "realized_shortfall": max(0, route["quoted_out"] - actual)}
