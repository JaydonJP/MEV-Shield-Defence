"""Executable local-devnet demonstrations of matching, backrun auctions and outflow budgets."""
from __future__ import annotations

import math
from web3.logs import DISCARD

from .. import config
from ..chain import BlockBuilder, account
from ..defense import amm_math


def _amount(value: float) -> int:
    if not math.isfinite(value) or not 0 < value <= 100_000:
        raise ValueError("amount must be finite and in (0, 100000]")
    return int(value * config.E)


def run_batch(chain, amount: float = 1000, match_fraction: float = .75) -> dict:
    usdc = _amount(amount)
    if not math.isfinite(match_fraction) or not .1 <= match_fraction <= 1:
        raise ValueError("match_fraction must be in [0.1, 1]")
    batch = chain.c("batch")
    pool = chain.c("ammA")
    price = pool.functions.spotPrice0().call()
    weth = int(usdc * match_fraction) * config.E // price
    # Alice sells USDC; Bob sells WETH. Limits are attached to each escrowed order.
    q_alice = pool.functions.quote(chain.deployments["contracts"]["USDC"], usdc).call()
    q_bob = pool.functions.quote(chain.deployments["contracts"]["WETH"], weth).call()
    deadline = chain.w3.eth.get_block("latest")["timestamp"] + 600
    id_a = batch.functions.count().call()
    receipts = [chain.transact(account("alice"), batch.functions.place(False, usdc, q_alice * 98 // 100, deadline))]
    id_b = batch.functions.count().call()
    receipts.append(chain.transact(account("bob"), batch.functions.place(True, weth, q_bob * 98 // 100, deadline)))
    before_a = chain.balance("WETH", account("alice").address)
    before_b = chain.balance("USDC", account("bob").address)
    # The next mined block closes the one-block collection window for both orders.
    rcpt = chain.transact(account("relay"), batch.functions.settle(id_b, id_a, price), gas=1_500_000)
    receipts.append(rcpt)
    event = batch.events.Settled().process_receipt(rcpt, errors=DISCARD)[0]["args"]
    return {"orders": [id_a, id_b], "clearing_price": price / config.E,
            "matched_weth": event["matched0"] / config.E, "matched_usdc": event["matched1"] / config.E,
            "alice_received_weth": (chain.balance("WETH", account("alice").address) - before_a) / config.E,
            "bob_received_usdc": (chain.balance("USDC", account("bob").address) - before_b) / config.E,
            "alice_amm_quote_weth": q_alice / config.E, "bob_amm_quote_usdc": q_bob / config.E,
            "matched_fraction": event["matched1"] / usdc,
            "gas_used": sum(r["gasUsed"] for r in receipts), "tx_hash": rcpt["transactionHash"].to_0x_hex(),
            "scope": "two escrowed orders, one pair, one clearing price; unmatched amounts use AMM"}


def run_cashback(chain, amount: float = 1000) -> dict:
    size = _amount(amount)
    auction = chain.c("cashback")
    d = chain.deployments["contracts"]
    a, b = chain.reserves("ammA"), chain.reserves("ammB")
    fee = chain.c("ammA").functions.feeBps().call()
    user_out = amm_math.amount_out(size, a[1], a[0], fee)
    after_a = (a[0] - user_out, a[1] + size)
    buy, sell = ("ammA", "ammB") if after_a[1] / after_a[0] < b[1] / b[0] else ("ammB", "ammA")
    pools = {"ammA": after_a, "ammB": b}
    best = amm_math.best_arbitrage(pools[buy], pools[sell], fee, flash_fee_bps=0)
    gas_estimate = chain.gas_cost_usd(700_000)
    if best["profit"] / config.E <= gas_estimate + .01:
        return {"settled": False, "reason": "no backrun profitable after estimated settlement gas"}
    capital, profit = best["amount"], best["profit"]
    deadline = chain.w3.eth.get_block("latest")["timestamp"] + 600
    order = auction.functions.orderHash(account("alice").address, d["ammA"], size, user_out * 98 // 100, deadline).call()
    bids = []
    for role, share in (("arbitrageur", 5000), ("relay", 7500)):
        rebate = profit * share // 10_000
        bid_id = auction.functions.count().call()
        rcpt = chain.transact(account(role), auction.functions.bid(d[buy], d[sell], capital, rebate, 0, deadline, order))
        bids.append({"id": bid_id, "searcher": role, "rebate_usdc": rebate / config.E,
                     "gas_used": rcpt["gasUsed"]})
    chosen = max(bids, key=lambda x: x["rebate_usdc"])
    before = chain.balance("USDC", account("alice").address)
    h = chain.send(account("alice"), auction.functions.settle(chosen["id"], d["ammA"], size,
                                                            user_out * 98 // 100, deadline), gas=1_500_000)
    BlockBuilder(chain).build_block()
    rcpt = chain.receipt(h)
    # Release every losing escrow, and the winning escrow too if settlement reverted.
    for bid in bids:
        if bid["id"] != chosen["id"] or rcpt["status"] != 1:
            chain.transact(account(bid["searcher"]), auction.functions.cancel(bid["id"]))
    if rcpt["status"] != 1:
        raise RuntimeError("cashback settlement reverted; all searcher escrows refunded")
    event = auction.events.Cashback().process_receipt(rcpt, errors=DISCARD)[0]["args"]
    actual_rebate = chain.balance("USDC", account("alice").address) - before + size
    return {"settled": True, "bids": bids, "winner": chosen["searcher"], "buy_pool": buy, "sell_pool": sell,
            "rebate_usdc": actual_rebate / config.E, "swap_out_weth": event["swapOut"] / config.E,
            "searcher_profit_usdc": event["profit"] / config.E,
            "settlement_gas_usd": rcpt["gasUsed"] * rcpt["effectiveGasPrice"] / config.E * chain.eth_price(),
            "tx_hash": h, "scope": "funded local bids; user swap executes before backrun atomically"}


def run_firewall(chain, amount: float = 8000) -> dict:
    size = _amount(amount)
    market = chain.c("firewall")
    alice = account("alice")
    chain.transact(alice, market.functions.deposit(100 * config.E))
    before = market.functions.liquidity().call()
    # Two independently valid borrows in the same block test the shared outflow cap.
    hashes = [chain.send(alice, market.functions.borrow(size), gas=500_000) for _ in range(2)]
    block = BlockBuilder(chain).build_block()
    statuses = [chain.receipt(h)["status"] == 1 for h in hashes]
    limits = market.functions.effectiveLimits().call()
    return {"requested_each_usdc": amount, "borrow_succeeded": statuses, "block": block.number,
            "block_limit_usdc": limits[0] / config.E, "window_limit_usdc": limits[1] / config.E,
            "window_blocks": market.functions.windowBlocks().call(),
            "window_used_usdc": market.functions.windowUsed().call() / config.E,
            "actual_outflow_usdc": (before - market.functions.liquidity().call()) / config.E,
            "tx_hashes": hashes, "scope": "on-chain global budgets plus TWAP/deviation checks"}
