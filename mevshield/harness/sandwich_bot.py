"""TEST HARNESS — rational sandwich bot (T1). Exists only to prove the shield works on the local devnet.

Watches the public mempool for swaps on the AMMs, computes the profit-maximising front-run with the exact
AMM math, validates the bundle [front, victim, back] on the shadow fork (as real searchers do), and only
attacks if the bundle is profitable after gas. Front-run tip > victim tip > back-run tip.
"""
from __future__ import annotations

from web3 import Web3

from .. import amm_math, config
from ..builder import Searcher
from ..chain import Chain, account
from ..shadow import ShadowFork, SimTx, fn_simtx, pending_to_simtx


def decode_swap(chain: Chain, tx: dict) -> dict | None:
    """Recognise a pending user swap. Returns {amm, token_in, amount_in, min_out} or None."""
    to = (tx.get("to") or "").lower()
    data = tx.get("input") or "0x"
    d = chain.deployments["contracts"]
    try:
        if to == d["router"].lower():
            fn, a = chain.c("router").decode_function_input(data)
            if fn.fn_name == "swapExactIn":
                return {"amm": a["amm"], "token_in": a["tokenIn"], "amount_in": a["amountIn"],
                        "min_out": a["minAmountOut"], "via": "router"}
        elif to == d["commitReveal"].lower():
            fn, a = chain.c("commitReveal").decode_function_input(data)
            if fn.fn_name == "reveal":
                return {"amm": a["amm"], "token_in": a["tokenIn"], "amount_in": a["amountIn"],
                        "min_out": a["minAmountOut"], "via": "reveal"}
        elif to in (d["ammA"].lower(), d["ammB"].lower()):
            key = "ammA" if to == d["ammA"].lower() else "ammB"
            fn, a = chain.c(key).decode_function_input(data)
            if fn.fn_name == "swap":
                return {"amm": Web3.to_checksum_address(to), "token_in": a["tokenIn"], "amount_in": a["amountIn"],
                        "min_out": a["minAmountOut"], "via": "amm"}
    except Exception:
        return None
    return None


class SandwichBot(Searcher):
    name = "sandwich_bot"

    def __init__(self, chain: Chain, shadow: ShadowFork | None = None, validate: bool = True, log=None):
        self.chain = chain
        self.shadow = shadow
        self.validate = validate and shadow is not None
        self.acct = account("sandwich_bot")
        self.attacked: set[str] = set()
        self.log = log or (lambda *a, **k: None)
        self.history: list[dict] = []

    def _amm_key(self, amm_addr: str) -> str:
        return self.chain.name_of(amm_addr)

    def on_pending(self, chain: Chain, pending: list[dict]) -> int:
        submitted = 0
        for tx in pending:
            h = tx["hash"].lower()
            if h in self.attacked or tx["from"].lower() == self.acct.address.lower():
                continue
            self.attacked.add(h)
            swap = decode_swap(chain, tx)
            if not swap:
                continue
            plan = self.plan(swap, tx)
            if plan:
                submitted += self.execute(plan, tx)
        return submitted

    def plan(self, swap: dict, victim_tx: dict) -> dict | None:
        c = self.chain
        amm_key = self._amm_key(swap["amm"])
        amm = c.c(amm_key)
        fee = amm.functions.feeBps().call()
        r0, r1 = c.reserves(amm_key)
        weth = c.deployments["contracts"]["WETH"].lower()
        token_in = swap["token_in"]
        token_out = c.deployments["contracts"]["USDC" if token_in.lower() == weth else "WETH"]
        r_in, r_out = (r0, r1) if token_in.lower() == weth else (r1, r0)
        in_sym = "WETH" if token_in.lower() == weth else "USDC"
        capital = c.balance(in_sym, c.deployments["contracts"]["sandwich"])
        best = amm_math.optimal_sandwich(r_in, r_out, swap["amount_in"], swap["min_out"], fee, capital=capital)
        price_in_usd = 1.0 if in_sym == "USDC" else r1 / r0
        victim_tip = int(victim_tx.get("maxPriorityFeePerGas", "0x0"), 16)
        front_tip = victim_tip + 1 * config.GWEI
        gas_usd = c.gas_cost_usd(config.SANDWICH_GAS, front_tip)
        profit_usd = best.profit / config.E * price_in_usd
        if best.frontrun <= 0 or profit_usd <= gas_usd:
            return None
        plan = {"amm": swap["amm"], "amm_key": amm_key, "token_in": token_in, "token_out": token_out,
                "frontrun": best.frontrun, "expected_profit_usd": profit_usd, "gas_usd": gas_usd,
                "front_tip": front_tip, "back_tip": victim_tip // 2, "victim": victim_tx["hash"],
                "victim_harm": best.harm, "in_sym": in_sym, "price_in_usd": price_in_usd}
        if self.validate:
            plan = self._validate(plan, victim_tx)
        return plan

    def _validate(self, plan: dict, victim_tx: dict) -> dict | None:
        """Simulate [front, victim, back] in one block on the shadow fork; abort if not profitable."""
        c = self.chain
        sw = c.c("sandwich")
        front = fn_simtx(self.acct.address, sw.functions.frontrun(plan["amm"], plan["token_in"], plan["frontrun"], 0))
        back = fn_simtx(self.acct.address, sw.functions.backrun(plan["amm"], plan["token_out"], 0))
        victim = pending_to_simtx(victim_tx)
        bal = lambda ch: ch.balance(plan["in_sym"], ch.deployments["contracts"]["sandwich"])
        self.shadow.sync()
        res = self.shadow.simulate([front, victim, back], before=bal, after=bal)
        if not all(r["status"] == 1 for r in res.receipts):
            self.log(f"[bot] bundle fails in simulation (victim reverts) — no attack on {plan['victim'][:10]}")
            return None
        sim_profit_usd = (res.after - res.before) / config.E * plan["price_in_usd"]
        if sim_profit_usd <= plan["gas_usd"]:
            return None
        plan["simulated_profit_usd"] = sim_profit_usd
        return plan

    def execute(self, plan: dict, victim_tx: dict) -> int:
        c = self.chain
        sw = c.c("sandwich")
        nonce = c.w3.eth.get_transaction_count(self.acct.address, "pending")
        hf = c.send(self.acct, sw.functions.frontrun(plan["amm"], plan["token_in"], plan["frontrun"], 0),
                    tip=plan["front_tip"], gas=600_000, nonce=nonce)
        hb = c.send(self.acct, sw.functions.backrun(plan["amm"], plan["token_out"], 0),
                    tip=plan["back_tip"], gas=600_000, nonce=nonce + 1)
        plan.update(front_hash=hf, back_hash=hb)
        self.history.append(plan)
        self.log(f"[bot] sandwiching {plan['victim'][:10]}: front-run {plan['frontrun'] / config.E:,.2f} "
                 f"{plan['in_sym']}, expected profit ${plan['expected_profit_usd']:,.2f}")
        return 2

