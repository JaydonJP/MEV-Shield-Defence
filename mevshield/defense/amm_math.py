"""Closed-form constant-product math, sandwich economics and attack-profit models.

Two flavours:
  * exact integer functions that replicate SimpleAMM.sol bit-for-bit (used by the fast bot/selector paths);
  * continuous closed-form formulas (used to cross-check the shadow-fork Risk Engine).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

FEE_DEN = 10_000


def amount_out(amount_in: int, r_in: int, r_out: int, fee_bps: int = 30) -> int:
    """Exact replica of SimpleAMM.getAmountOut."""
    if amount_in <= 0:
        return 0
    with_fee = amount_in * (FEE_DEN - fee_bps)
    return with_fee * r_out // (r_in * FEE_DEN + with_fee)


def amount_out_f(x: float, r_in: float, r_out: float, fee_bps: int = 30) -> float:
    g = 1 - fee_bps / FEE_DEN
    return g * x * r_out / (r_in + g * x)


@dataclass
class SandwichResult:
    frontrun: int  # attacker input (token_in)
    front_out: int
    victim_out: int  # what the victim receives when sandwiched
    benign_out: int  # what the victim receives alone
    back_out: int  # attacker's token_in recovered
    profit: int  # back_out - frontrun (token_in units)
    victim_reverts: bool

    @property
    def harm(self) -> int:
        return 0 if self.victim_reverts else self.benign_out - self.victim_out


def simulate_sandwich(r_in: int, r_out: int, victim_in: int, min_out: int, f: int, fee_bps: int = 30) -> SandwichResult:
    """Exact integer simulation of [frontrun f, victim, backrun] on one pool. All amounts in token units."""
    benign = amount_out(victim_in, r_in, r_out, fee_bps)
    if f <= 0:
        return SandwichResult(0, 0, benign, benign, 0, 0, benign < min_out)
    fo = amount_out(f, r_in, r_out, fee_bps)
    r_in1, r_out1 = r_in + f, r_out - fo
    vo = amount_out(victim_in, r_in1, r_out1, fee_bps)
    reverts = vo < min_out
    if reverts:  # victim reverts: attacker just round-trips
        r_in2, r_out2 = r_in1, r_out1
        vo_eff = 0
    else:
        r_in2, r_out2 = r_in1 + victim_in, r_out1 - vo
        vo_eff = vo
    back = amount_out(fo, r_out2, r_in2, fee_bps)
    return SandwichResult(f, fo, vo_eff, benign, back, back - f, reverts)


def max_frontrun_without_revert(r_in: int, r_out: int, victim_in: int, min_out: int, fee_bps: int = 30) -> int:
    """Largest f such that the victim still receives >= min_out (binary search, exact)."""
    if amount_out(victim_in, r_in, r_out, fee_bps) < min_out:
        return 0
    lo, hi = 0, max(1, r_in)
    while amount_out(victim_in, r_in + hi, r_out - amount_out(hi, r_in, r_out, fee_bps), fee_bps) >= min_out:
        hi *= 2
        if hi > r_in * 1000:
            return hi
    while hi - lo > max(1, lo // 10**9):
        mid = (lo + hi) // 2
        vo = amount_out(victim_in, r_in + mid, r_out - amount_out(mid, r_in, r_out, fee_bps), fee_bps)
        if vo >= min_out:
            lo = mid
        else:
            hi = mid
    return lo


def golden_section_max(fn, lo: float, hi: float, iters: int = 40) -> tuple[float, float]:
    """Maximise a unimodal function on [lo, hi]. Returns (argmax, max)."""
    phi = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - phi * (b - a), a + phi * (b - a)
    fc, fd = fn(c), fn(d)
    for _ in range(iters):
        if fc >= fd:
            b, d, fd = d, c, fc
            c = b - phi * (b - a)
            fc = fn(c)
        else:
            a, c, fc = c, d, fd
            d = a + phi * (b - a)
            fd = fn(d)
    x = (a + b) / 2
    best = max(((x, fn(x)), (lo, fn(lo)), (hi, fn(hi))), key=lambda t: t[1])
    return best


def optimal_sandwich(r_in: int, r_out: int, victim_in: int, min_out: int, fee_bps: int = 30,
                     capital: int | None = None) -> SandwichResult:
    """Profit-maximising front-run subject to the victim not reverting (exact integer model)."""
    f_max = max_frontrun_without_revert(r_in, r_out, victim_in, min_out, fee_bps)
    if capital is not None:
        f_max = min(f_max, capital)
    if f_max <= 0:
        return simulate_sandwich(r_in, r_out, victim_in, min_out, 0, fee_bps)
    f_star, _ = golden_section_max(
        lambda f: simulate_sandwich(r_in, r_out, victim_in, min_out, int(f), fee_bps).profit, 0, f_max, iters=60
    )
    return simulate_sandwich(r_in, r_out, victim_in, min_out, int(f_star), fee_bps)


def closed_form_sandwich(r_in: float, r_out: float, victim_in: float, slippage: float, fee_bps: int = 30) -> dict:
    """Continuous-math sandwich optimum — the textbook formula used to validate the simulator.

    The victim's minimum output is (1 - s)·out_benign. The front-run f* is the larger of
    (a) the interior optimum of attacker profit and (b) the boundary where the victim hits minOut.
    The boundary solves  out(V | r_in+f, r_out-out(f)) = minOut, i.e. a quadratic in f.
    """
    g = 1 - fee_bps / FEE_DEN
    benign = amount_out_f(victim_in, r_in, r_out, fee_bps)
    min_out = (1 - slippage) * benign
    k = r_in * r_out

    # boundary: with x = r_in + f and y ≈ k'/x (fees on f ignored for the boundary, as in the literature),
    # victim out = g·V·y/(x + g·V) = minOut  →  g·V·k/x = minOut·(x + g·V)  →  quadratic in x.
    a, b, c = min_out, min_out * g * victim_in, -g * victim_in * k
    x = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)
    f_bound = max(0.0, x - r_in)

    def profit(f: float) -> float:
        fo = amount_out_f(f, r_in, r_out, fee_bps)
        ri, ro = r_in + f, r_out - fo
        vo = amount_out_f(victim_in, ri, ro, fee_bps)
        if vo < min_out * (1 - 1e-12):
            return -1e30
        return amount_out_f(fo, ro - vo, ri + victim_in, fee_bps) - f

    f_star, p = golden_section_max(profit, 0.0, max(f_bound, 1e-9), iters=80)
    fo = amount_out_f(f_star, r_in, r_out, fee_bps)
    vo = amount_out_f(victim_in, r_in + f_star, r_out - fo, fee_bps)
    return {"frontrun": f_star, "profit": p, "victim_out": vo, "benign_out": benign, "harm": benign - vo,
            "f_bound": f_bound}


def max_safe_slippage(r_in: int, r_out: int, victim_in: int, fee_bps: int, cost_in_token: float,
                      hi: float = 0.2) -> float:
    """Largest slippage tolerance s* at which the best sandwich is NOT profitable (profit <= attacker cost)."""
    benign = amount_out(victim_in, r_in, r_out, fee_bps)

    def profitable(s: float) -> bool:
        min_out = int(benign * (1 - s))
        res = optimal_sandwich(r_in, r_out, victim_in, min_out, fee_bps)
        return res.profit > cost_in_token

    if not profitable(hi):
        return hi
    lo = 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if profitable(mid):
            hi = mid
        else:
            lo = mid
    return lo


# ---------------------------------------------------------------------- T2 / T3
def oracle_attack_profit(r_weth: int, r_usdc: int, flash: int, liquidity: int, fee_bps: int = 30,
                         ltv_bps: int = 7500, flash_fee_bps: int = 9) -> dict:
    """Outcome of the spot-price oracle attack on VulnerableLending for a given flash-loan size."""
    bought = amount_out(flash, r_usdc, r_weth, fee_bps)
    r_w2, r_u2 = r_weth - bought, r_usdc + flash
    spot = r_u2 * 10**18 // r_w2
    borrow = min(bought * spot // 10**18 * ltv_bps // 10_000, liquidity)
    fee = flash * flash_fee_bps // 10_000
    return {"flash": flash, "collateral": bought, "manipulated_price": spot, "borrow": borrow,
            "profit": borrow - flash - fee}


def best_oracle_attack(r_weth: int, r_usdc: int, liquidity: int, max_flash: int) -> dict:
    best = None
    for i in range(1, 201):
        flash = max_flash * i // 200
        res = oracle_attack_profit(r_weth, r_usdc, flash, liquidity)
        if best is None or res["profit"] > best["profit"]:
            best = res
    return best


def best_arbitrage(buy_pool: tuple[int, int], sell_pool: tuple[int, int], fee_bps: int = 30,
                   flash_fee_bps: int = 9) -> dict:
    """Buy token0 (WETH) with token1 (USDC) on `buy_pool`, sell on `sell_pool`. Pools are (r0, r1)."""
    def profit(x: float) -> float:
        x = int(x)
        got = amount_out(x, buy_pool[1], buy_pool[0], fee_bps)
        back = amount_out(got, sell_pool[0], sell_pool[1], fee_bps)
        return back - x - x * flash_fee_bps // 10_000

    x, p = golden_section_max(profit, 0, buy_pool[1] // 4, iters=60)
    return {"amount": int(x), "profit": int(p)}
