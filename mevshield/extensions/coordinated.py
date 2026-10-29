"""Bounded transaction graph + counterfactual sequence detector.

Shared pools create candidates, not proof of shared ownership. Actual grouped execution must harm the
victim and profit the candidate actors after gas. Explicit token delivery or a shared sender strengthens
the finding. This covers modeled local sandwiches, not arbitrary cross-chain attack attribution.
"""
from __future__ import annotations

from itertools import combinations
from time import perf_counter

from .. import config
from ..chain import BlockBuilder, account, pending_to_simtx
from ..defense import amm_math
from ..harness.sandwich_bot import decode_swap


def ordered_pending(pending: list[dict], max_transactions: int = 32) -> list[dict]:
    if len(pending) > max_transactions:
        raise ValueError(f"sequence detector accepts at most {max_transactions} transactions")
    def integer(x):
        return int(x, 16) if isinstance(x, str) else int(x)
    remaining = list(pending)
    result = []
    while remaining:
        # Respect each sender's nonce order before comparing fee priority between senders.
        eligible = [t for t in remaining if not any(
            u["from"].lower() == t["from"].lower() and integer(u.get("nonce", 0)) < integer(t.get("nonce", 0))
            for u in remaining)]
        tx = max(eligible, key=lambda t: integer(t.get("maxPriorityFeePerGas", 0)))
        result.append(tx)
        remaining.remove(tx)
    return result


def decode_action(chain, tx: dict) -> dict | None:
    sw = decode_swap(chain, tx)
    if sw:
        recipient = tx["from"]
        key = chain.name_of(tx.get("to"))
        if key in ("ammA", "ammB"):
            _, args = chain.c(key).decode_function_input(tx.get("input") or tx.get("data"))
            recipient = args["to"]
        return {**sw, "recipient": recipient}
    if (tx.get("to") or "").lower() == chain.deployments["contracts"].get("sandwich", "").lower():
        fn, args = chain.c("sandwich").decode_function_input(tx.get("input") or tx.get("data"))
        if fn.fn_name in ("frontrun", "backrun"):
            return {"amm": args["amm"], "token_in": args["tokenIn"], "amount_in": args.get("amountIn", 0),
                    "min_out": args["minOut"], "recipient": tx["to"], "via": fn.fn_name}
    return None


def analyze_sequences(chain, shadow, pending: list[dict], max_candidates: int = 24) -> dict:
    start = perf_counter()
    txs = ordered_pending(pending)
    actions = [decode_action(chain, t) for t in txs]
    nodes = [{"hash": t["hash"], "sender": t["from"], "position": i,
              "pool": a["amm"] if a else None, "token_in": a["token_in"] if a else None}
             for i, (t, a) in enumerate(zip(txs, actions))]
    edges, candidates = [], []
    for i, j in combinations(range(len(txs)), 2):
        a, b = actions[i], actions[j]
        if a and b and a["amm"].lower() == b["amm"].lower():
            edges.append({"source": txs[i]["hash"], "target": txs[j]["hash"], "kind": "shared_pool"})
            if a["recipient"].lower() == txs[j]["from"].lower():
                edges.append({"source": txs[i]["hash"], "target": txs[j]["hash"], "kind": "token_delivery"})
    truncated = False
    for i, j, k in combinations(range(len(txs)), 3):
        a, v, b = actions[i], actions[j], actions[k]
        if not (a and v and b):
            continue
        if not (a["amm"].lower() == v["amm"].lower() == b["amm"].lower() and
                a["token_in"].lower() == v["token_in"].lower() != b["token_in"].lower()):
            continue
        if txs[j]["from"].lower() in (txs[i]["from"].lower(), txs[k]["from"].lower()):
            continue
        if len(candidates) >= max_candidates:
            truncated = True
            break
        candidates.append((i, j, k))
    findings = []
    shadow.sync()
    reference = chain.eth_price()
    for i, j, k in candidates:
        a, v, b = actions[i], actions[j], actions[k]
        weth_in = v["token_in"].lower() == chain.deployments["contracts"]["WETH"].lower()
        out_token = "USDC" if weth_in else "WETH"
        victim = v["recipient"]
        # Harness contracts can retain the profit; include them as actors as well as their callers.
        actors = {txs[i]["from"], txs[k]["from"]}
        for index in (i, k):
            if actions[index]["via"] in ("frontrun", "backrun"):
                actors.add(txs[index]["to"])
        def state(ch):
            return {"victim": ch.balance(out_token, victim),
                    "actors": sum(ch.balance("USDC", x) + ch.balance("WETH", x) * reference for x in actors)}
        alone = shadow.simulate([pending_to_simtx(txs[j])], before=state, after=state)
        grouped = shadow.simulate([pending_to_simtx(txs[x]) for x in (i, j, k)], before=state, after=state)
        if not all(r["status"] == 1 for r in alone.receipts + grouped.receipts):
            continue
        alone_out = alone.after["victim"] - alone.before["victim"]
        grouped_out = grouped.after["victim"] - grouped.before["victim"]
        harm = (alone_out - grouped_out) / config.E * (1 if weth_in else reference)
        actor_profit = (grouped.after["actors"] - grouped.before["actors"]) / config.E
        gas = chain.gas_cost_usd(sum(grouped.receipts[x]["gasUsed"] for x in (0, 2)), 2 * config.GWEI)
        linked = (txs[i]["from"].lower() == txs[k]["from"].lower() or
                  a["recipient"].lower() == txs[k]["from"].lower())
        if harm > .01 and actor_profit > gas:
            findings.append({"label": "CRITICAL" if linked else "SUSPICIOUS",
                             "hashes": [txs[x]["hash"] for x in (i, j, k)],
                             "different_senders": txs[i]["from"].lower() != txs[k]["from"].lower(),
                             "explicit_link": linked, "victim_harm_usd": harm,
                             "actor_profit_usd": actor_profit, "actor_gas_usd": gas,
                             "evidence": "grouped simulation vs victim alone; profit net of estimated gas"})
    return {"nodes": nodes, "edges": edges, "findings": findings, "candidates_tested": len(candidates),
            "truncated": truncated, "latency_ms": (perf_counter() - start) * 1000,
            "ordering_assumption": "local fee priority constrained by sender nonces; no ownership inference from pool alone"}


def run_coordinated(chain, shadow, amount: float = 10000) -> dict:
    from math import isfinite
    if not isfinite(amount) or not 1 <= amount <= 100_000:
        raise ValueError("amount must be in [1, 100000]")
    d = chain.deployments["contracts"]
    front_actor, back_actor = account("oracle_attacker"), account("gov_attacker")
    size = int(amount * config.E)
    pool = chain.c("ammA")
    r0, r1 = chain.reserves("ammA")
    fee = pool.functions.feeBps().call()
    quote = amm_math.amount_out(size, r1, r0, fee)
    model = amm_math.optimal_sandwich(r1, r0, size, quote * 98 // 100, fee, capital=50_000 * config.E)
    front = model.frontrun
    if front <= 0:
        raise RuntimeError("no feasible coordinated sandwich")
    chain.transact(account("admin"), chain.c("USDC").functions.mint(front_actor.address, front))
    chain.transact(front_actor, chain.c("USDC").functions.approve(d["ammA"], front))
    chain.transact(back_actor, chain.c("WETH").functions.approve(d["ammA"], 2**256 - 1))
    # Front-run delivers the purchased WETH directly to a different account which performs the backrun.
    hashes = [chain.send(front_actor, pool.functions.swap(d["USDC"], front, 0, back_actor.address),
                         tip=3 * config.GWEI, gas=600_000)]
    hashes.append(chain.send(account("alice"), chain.c("router").functions.swapExactIn(
        d["ammA"], d["USDC"], size, quote * 98 // 100,
        chain.w3.eth.get_block("latest")["timestamp"] + 300), tip=2 * config.GWEI, gas=600_000))
    bought = amm_math.amount_out(front, r1, r0, fee)
    hashes.append(chain.send(back_actor, pool.functions.swap(d["WETH"], bought, 0, front_actor.address),
                             tip=config.GWEI, gas=600_000))
    pending = chain.txpool()
    try:
        result = analyze_sequences(chain, shadow, pending)
    finally:
        # Detector failures must not leave a live attack sitting in the devnet mempool.
        chain.drop_all_pending()
    result["submitted_hashes"] = hashes
    result["scope"] = "counterfactual only; the attack transactions were dropped after analysis"
    return result
