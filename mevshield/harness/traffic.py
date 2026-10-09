"""Benign traffic generator: ordinary swaps and ordinary lending, used by the benchmark workload."""
from __future__ import annotations

import random

from .. import config
from ..chain import Chain, account


def submit_user_swap(chain: Chain, role: str, amount: float, token_in: str = "USDC", amm: str = "ammA",
                     slippage: float = config.DEFAULT_SLIPPAGE, tip: int = config.DEFAULT_TIP,
                     min_out: int | None = None) -> tuple[str, int, int]:
    """Public swap through the ProtectedRouter. Returns (hash, quoted_out, min_out)."""
    d = chain.deployments["contracts"]
    amount_in = int(amount * config.E)
    quoted = chain.c(amm).functions.quote(d[token_in], amount_in).call()
    if min_out is None:
        min_out = int(quoted * (1 - slippage))
    fn = chain.c("router").functions.swapExactIn(d[amm], d[token_in], amount_in, min_out, chain.w3.eth.get_block("latest")["timestamp"] + 3600)
    return chain.send(account(role), fn, tip=tip, gas=600_000), quoted, min_out


def submit_borrow(chain: Chain, role: str, weth: float, market: str = "vulnerable",
                  tip: int = config.DEFAULT_TIP) -> list[str]:
    """Benign borrower: deposit WETH and borrow 50 % of its value (well inside the 75 % LTV)."""
    acct = account(role)
    m = chain.c(market)
    amt = int(weth * config.E)
    price = chain.c("ammA").functions.spotPrice0().call()
    borrow = amt * price // config.E // 2
    nonce = chain.w3.eth.get_transaction_count(acct.address, "pending")
    h1 = chain.send(acct, m.functions.deposit(amt), tip=tip, gas=500_000, nonce=nonce)
    h2 = chain.send(acct, m.functions.borrow(borrow), tip=tip, gas=500_000, nonce=nonce + 1)
    return [h1, h2]


def random_swap_size(rng: random.Random) -> float:
    """Log-uniform trade sizes in USDC from $5 to $20k (a long-tailed retail/whale mix)."""
    return round(10 ** rng.uniform(0.7, 4.3), 2)


def benign_mix(rng: random.Random) -> dict:
    roll = rng.random()
    if roll < 0.80:
        token = "USDC" if rng.random() < 0.6 else "WETH"
        size = random_swap_size(rng)
        if token == "WETH":
            size = round(size / 2000, 4)
        return {"kind": "swap", "role": rng.choice(["bob", "carol"]), "token_in": token, "amount": size,
                "amm": "ammA" if rng.random() < 0.75 else "ammB",
                "slippage": rng.choice([0.005, 0.01, 0.02, 0.03])}
    return {"kind": "borrow", "role": rng.choice(["bob", "carol"]), "weth": round(rng.uniform(0.5, 5), 2)}
