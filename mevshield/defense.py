"""User-side defenses and the Defense Selector.

    J(d) = Harm_d + λ1·GasCost_d + λ2·Latency_d + λ3·RevertRisk_d        → choose argmin_d J(d)

public            send normally
adaptive-slippage set minAmountOut at the largest slippage s* at which the best sandwich is unprofitable
private-relay     skip the public mempool (relay fee, trust in relay)
commit-reveal     commit hash in block N, reveal in N+1 (2 txs, +1 block)
split             n chunks over n blocks
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import amm_math, config
from .builder import BlockBuilder, BlockSummary
from .chain import Chain, account
from .relay import PrivateRelay
from .risk import RiskEngine, RiskReport, SwapIntent, revert_probability

ADAPTIVE_MARGIN = 0.85  # stay safely below the attacker's break-even slippage
DEFENSES = ["public", "adaptive-slippage", "private-relay", "commit-reveal", "split"]


@dataclass
class DefenseEval:
    name: str
    harm: float
    gas: float
    latency_blocks: float
    revert_risk: float  # probability
    revert_cost: float  # expected USD
    fee: float = 0.0
    params: dict = field(default_factory=dict)

    @property
    def J(self) -> float:
        return (self.harm + config.LAMBDA_GAS * (self.gas + self.fee)
                + config.LAMBDA_LATENCY_PER_BLOCK * self.latency_blocks + self.revert_cost)


@dataclass
class Selection:
    report: RiskReport
    evals: list[DefenseEval]

    @property
    def best(self) -> DefenseEval:
        return min(self.evals, key=lambda e: e.J)

    def get(self, name: str) -> DefenseEval:
        return next(e for e in self.evals if e.name == name)


class DefenseSelector:
    def __init__(self, chain: Chain, engine: RiskEngine):
        self.chain = chain
        self.engine = engine

    def evaluate(self, intent: SwapIntent, mode: str = "sim", report: RiskReport | None = None) -> Selection:
        c = self.chain
        rep = report or self.engine.assess(intent, mode=mode)
        ex = rep.extra
        r_in, r_out, fee, amount_in = ex["r_in"], ex["r_out"], ex["ctx_fee"], ex["amount_in"]
        swap_gas = c.gas_cost_usd(config.SWAP_GAS, intent.tip)
        retry = lambda gas_usd: config.RETRY_COST + gas_usd
        s = intent.slippage
        evals = []

        # public
        p_pub = revert_probability(s)
        evals.append(DefenseEval("public", rep.harm_public_usd, swap_gas, 0, p_pub, p_pub * retry(swap_gas),
                                 params={"slippage": s}))

        # adaptive slippage
        cost_tok = rep.attacker_gas_usd / rep.in_price_usd * config.E
        s_star = amm_math.max_safe_slippage(r_in, r_out, amount_in, fee, cost_tok, hi=max(s, 1e-6))
        s_ad = min(s, s_star * ADAPTIVE_MARGIN)
        benign = amm_math.amount_out(amount_in, r_in, r_out, fee)
        min_out_ad = int(benign * (1 - s_ad))
        harm_ad = 0.0
        if s_ad < s and mode == "sim":  # verify on the shadow fork: is the best attack at s* unprofitable?
            check = self.engine.assess(intent, mode="sim", leakage_levels=False, min_out=min_out_ad)
            harm_ad = check.harm_public_usd
        elif s_ad >= s:
            harm_ad = rep.harm_public_usd
        p_ad = revert_probability(s_ad)
        evals.append(DefenseEval("adaptive-slippage", harm_ad, swap_gas, 0, p_ad, p_ad * retry(swap_gas),
                                 params={"slippage": s_ad, "s_star": s_star, "min_out": min_out_ad}))

        # private relay
        evals.append(DefenseEval("private-relay", rep.harm_private_usd, swap_gas, 0, p_pub, p_pub * retry(swap_gas),
                                 fee=config.RELAY_FEE_USD, params={"slippage": s}))

        # commit-reveal: two txs, one extra block, slippage room must survive two blocks of drift
        cr_gas = c.gas_cost_usd(config.COMMIT_GAS, intent.tip) + c.gas_cost_usd(config.SWAP_GAS + 30_000, 3 * config.GWEI)
        p_cr = revert_probability(s, blocks=2)
        evals.append(DefenseEval("commit-reveal", 0.0, cr_gas, 1, p_cr, p_cr * retry(cr_gas), params={"slippage": s}))

        # split into n chunks across n blocks (public leakage each)
        best_split = None
        for n in (2, 3, 4, 5):
            harm, ri, ro = 0.0, r_in, r_out
            chunk = amount_in // n
            for _ in range(n):
                b = amm_math.amount_out(chunk, ri, ro, fee)
                res = amm_math.optimal_sandwich(ri, ro, chunk, int(b * (1 - s)), fee)
                if res.profit / config.E * rep.in_price_usd > rep.attacker_gas_usd:
                    harm += res.harm / config.E * rep.out_price_usd
                ri, ro = ri + chunk, ro - b
            p = 1 - (1 - p_pub) ** n
            ev = DefenseEval("split", harm, n * swap_gas, n - 1, p, p * retry(swap_gas), params={"n": n, "slippage": s})
            if best_split is None or ev.J < best_split.J:
                best_split = ev
        evals.append(best_split)
        return Selection(rep, evals)


# ------------------------------------------------------------------ execution
@dataclass
class ExecutionResult:
    defense: str
    tx_hashes: list[str]
    blocks: list[BlockSummary]
    expected_out: int
    actual_out: int
    out_price_usd: float
    reverted: bool = False

    @property
    def harm_usd(self) -> float:
        if self.reverted:
            return 0.0
        return max(0.0, (self.expected_out - self.actual_out) / config.E * self.out_price_usd)

    @property
    def sandwiched(self) -> bool:
        for blk in self.blocks:
            fns = [t.fn for t in blk.txs]
            if any(f.endswith("frontrun") for f in fns):
                return True
        return False


def execute(chain: Chain, builder: BlockBuilder, intent: SwapIntent, defense: str, params: dict | None = None,
            log=print) -> ExecutionResult:
    params = params or {}
    d = chain.deployments["contracts"]
    acct = account(intent.role)
    user = acct.address
    amm = chain.c(intent.amm)
    amount_in = int(intent.amount * config.E)
    expected = amm.functions.quote(d[intent.token_in], amount_in).call()
    before = chain.balance(intent.token_out, user)
    r0, r1 = chain.reserves(intent.amm)
    out_px = r1 / r0 if intent.token_out == "WETH" else 1.0  # price *before* execution
    deadline = lambda: chain.w3.eth.get_block("latest")["timestamp"] + 3600
    router = chain.c("router")
    hashes, blocks = [], []

    def min_out_for(q: int, slip: float) -> int:
        return int(q * (1 - slip))

    if defense in ("public", "adaptive-slippage"):
        slip = params.get("slippage", intent.slippage)
        fn = router.functions.swapExactIn(d[intent.amm], d[intent.token_in], amount_in, min_out_for(expected, slip),
                                          deadline())
        hashes.append(chain.send(acct, fn, tip=intent.tip, gas=600_000))
        log(f"  → public mempool: swap {intent.amount:,} {intent.token_in} (minOut at {slip * 100:.3f} % slippage)")
        blocks.append(builder.build_block())

    elif defense == "private-relay":
        fn = router.functions.swapExactIn(d[intent.amm], d[intent.token_in], amount_in,
                                          min_out_for(expected, intent.slippage), deadline())
        hashes.append(PrivateRelay(chain).submit(acct, fn, tip=intent.tip, gas=600_000))
        log("  → private relay: tx held off the public mempool until the block is sealed")
        blocks.append(builder.build_block())

    elif defense == "commit-reveal":
        cr = chain.c("commitReveal")
        salt = os.urandom(32)
        min_out = min_out_for(expected, intent.slippage)
        h = cr.functions.commitmentHash(user, d[intent.amm], d[intent.token_in], amount_in, min_out, salt).call()
        hashes.append(chain.send(acct, cr.functions.commit(h), tip=intent.tip, gas=200_000))
        log(f"  → block N: commit {h.hex()[:18]}… (reveals nothing about the order)")
        blocks.append(builder.build_block())
        hashes.append(chain.send(acct, cr.functions.reveal(d[intent.amm], d[intent.token_in], amount_in, min_out, salt),
                                 tip=3 * config.GWEI, gas=600_000))
        log("  → block N+1: reveal + execute (must be the first trade on the pool in its block)")
        blocks.append(builder.build_block())

    elif defense == "split":
        n = params.get("n", 3)
        chunk = amount_in // n
        for i in range(n):
            q = amm.functions.quote(d[intent.token_in], chunk).call()
            fn = router.functions.swapExactIn(d[intent.amm], d[intent.token_in], chunk,
                                              min_out_for(q, intent.slippage), deadline())
            hashes.append(chain.send(acct, fn, tip=intent.tip, gas=600_000))
            log(f"  → chunk {i + 1}/{n}: {chunk / config.E:,.2f} {intent.token_in}")
            blocks.append(builder.build_block())
    else:
        raise ValueError(defense)

    actual = chain.balance(intent.token_out, user) - before
    statuses = [chain.receipt(h)["status"] for h in hashes]
    return ExecutionResult(defense, hashes, blocks, expected, actual, out_px, reverted=not all(statuses))
