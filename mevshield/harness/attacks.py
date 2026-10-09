"""TEST HARNESS — flash-loan oracle attack (T2), flash-loan arbitrage (T3) and governance attack (T4).

These only build and submit transactions against the contracts deployed on the local devnet so that the
Guardian and the by-design defenses can be demonstrated and measured.
"""
from __future__ import annotations

from .. import amm_math, config
from ..chain import Chain, account

TARGETS = {"vulnerable": "vulnerable", "safe": "safe"}


def plan_oracle_attack(chain: Chain, target: str = "vulnerable") -> dict:
    """Choose the flash-loan size that maximises profit against a spot-priced lending market."""
    r_weth, r_usdc = chain.reserves("ammA")
    liquidity = chain.c(target).functions.liquidity().call()
    max_flash = chain.c("lender").functions.maxFlashLoan(chain.deployments["contracts"]["USDC"]).call()
    best = amm_math.best_oracle_attack(r_weth, r_usdc, liquidity, min(max_flash, 4 * r_usdc))
    if best["profit"] <= 0:  # still launch something plausible (e.g. against SafeLending)
        best = amm_math.oracle_attack_profit(r_weth, r_usdc, int(2.1 * r_usdc), liquidity)
    return best


def submit_oracle_attack(chain: Chain, target: str = "vulnerable", tip: int = 2 * config.GWEI) -> tuple[str, dict]:
    plan = plan_oracle_attack(chain, target)
    acct = account("oracle_attacker")
    fn = chain.c("oracleAttacker").functions.attack(chain.deployments["contracts"][target], plan["flash"])
    return chain.send(acct, fn, tip=tip, gas=1_200_000), plan


def plan_arbitrage(chain: Chain) -> dict | None:
    a, b = chain.reserves("ammA"), chain.reserves("ammB")
    pa, pb = a[1] / a[0], b[1] / b[0]
    buy, sell = ("ammA", "ammB") if pa < pb else ("ammB", "ammA")
    best = amm_math.best_arbitrage(chain.reserves(buy), chain.reserves(sell), config.POOL_FEE_BPS)
    if best["profit"] <= 0:
        return None
    return {"buy": buy, "sell": sell, **best, "gap": abs(pa - pb) / min(pa, pb)}


def submit_arbitrage(chain: Chain, tip: int = 2 * config.GWEI) -> tuple[str | None, dict | None]:
    plan = plan_arbitrage(chain)
    if not plan:
        return None, None
    d = chain.deployments["contracts"]
    fn = chain.c("flashArb").functions.execute(d[plan["buy"]], d[plan["sell"]], d["USDC"], d["WETH"],
                                                plan["amount"], 0)
    return chain.send(account("arbitrageur"), fn, tip=tip, gas=800_000), plan


def submit_governance_attack(chain: Chain, target: str = "vulnerableDAO", tip: int = 2 * config.GWEI) -> tuple[str, dict]:
    d = chain.deployments["contracts"]
    flash = 550_000 * config.E
    fn = chain.c("govAttacker").functions.attack(d[target], d["USDC"], flash)
    treasury = chain.balance("USDC", d[target])
    return chain.send(account("gov_attacker"), fn, tip=tip, gas=1_500_000), {"flash": flash, "treasury": treasury}
