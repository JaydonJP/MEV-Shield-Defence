"""Counterfactual Risk Engine (user side): "is my swap going to be sandwiched, and how badly?"

For a swap intent, on the shadow fork:
  1. execute the swap alone                                   -> out_benign
  2. a rational attacker who sees the tx (leakage = public) searches the front-run f (golden-section)
     maximising profit subject to the victim not reverting and profit > gas  -> out_attacked
  3. predicted harm  H = out_benign - out_attacked   (the counterfactual-harm definition)
  4. repeat for leakage = hint (pool + direction only) and private
  5. cross-check against the closed-form constant-product sandwich formula
`mode="fast"` replaces the shadow fork with the bit-exact integer AMM model (for benchmarks / the selector).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import asdict, dataclass, field

from .. import config
from . import amm_math
from ..chain import Chain, account
from ..chain import ShadowFork, fn_simtx


@dataclass
class SwapIntent:
    amount: float  # in token_in units
    token_in: str = "USDC"
    amm: str = "ammA"
    slippage: float = config.DEFAULT_SLIPPAGE
    role: str = "alice"
    tip: int = config.DEFAULT_TIP

    def __post_init__(self):
        if (
            not math.isfinite(self.amount)
            or self.amount <= 0
            or not math.isfinite(self.slippage)
            or not 0 < self.slippage <= 0.2
        ):
            raise ValueError("Invalid swap amount or slippage")
        if (
            self.token_in not in ("USDC", "WETH")
            or self.amm not in ("ammA", "ammB")
            or self.role not in config.ROLES
        ):
            raise ValueError("Unsupported local swap intent")
        if type(self.tip) is not int or self.tip < 0:
            raise ValueError("Invalid priority fee")

    @property
    def token_out(self) -> str:
        return "WETH" if self.token_in == "USDC" else "USDC"


@dataclass
class RiskReport:
    intent: SwapIntent
    out_benign: int
    min_out: int
    frontrun: int | None
    attacker_profit_usd: float | None
    attacker_gas_usd: float
    attack_profitable: bool | None
    out_attacked: int | None
    harm_public_usd: float
    harm_hint_usd: float | None
    harm_private_usd: float
    p_attack: float
    closed_form_harm_usd: float | None
    closed_form_error_pct: float | None
    out_price_usd: float
    in_price_usd: float
    sim_ms: float
    mode: str
    evals: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def trade_usd(self) -> float:
        return self.intent.amount * self.in_price_usd

    def as_dict(self) -> dict:
        d = asdict(self)
        d["intent"] = asdict(self.intent)
        d["trade_usd"] = self.trade_usd
        return d


class RiskEngine:
    def __init__(
        self,
        chain: Chain,
        shadow: ShadowFork | None = None,
        iters: int = 12,
        predictor=None,
        policy=None,
    ):
        self.chain = chain
        self.shadow = shadow
        self.iters = iters
        self.predictor = predictor
        self.policy = policy
        self.stats = dict(
            ml_calls=0,
            ml_ms=0.0,
            shadow_bundles=0,
            shadow_transactions=0,
            shadow_ms=0.0,
        )

    # ------------------------------------------------------------ context
    def context(self, intent: SwapIntent) -> dict:
        c = self.chain
        block = c.w3.eth.get_block("latest")
        d = c.deployments["contracts"]
        r0, r1 = c.reserves(intent.amm)
        fee = c.c(intent.amm).functions.feeBps().call()
        r_in, r_out = (r1, r0) if intent.token_in == "USDC" else (r0, r1)
        price_weth = r1 / r0
        in_px = 1.0 if intent.token_in == "USDC" else price_weth
        out_px = price_weth if intent.token_in == "USDC" else 1.0
        amount_in = int(intent.amount * config.E)
        benign = amm_math.amount_out(amount_in, r_in, r_out, fee)
        base_fee = c.base_fee()
        gas_eth_price = c.eth_price()
        capital = c.balance(intent.token_in, d["sandwich"])
        state = {
            "block_hash": block["hash"].to_0x_hex(),
            "block": block["number"],
            "r_in": r_in,
            "r_out": r_out,
            "fee": fee,
            "base_fee": base_fee,
            "gas_eth_price": gas_eth_price,
            "capital": capital,
            "intent": asdict(intent),
        }
        if c.w3.eth.get_block("latest")["hash"] != block["hash"]:
            raise RuntimeError("Chain changed while reading risk context; retry")
        return {
            "d": d,
            "r_in": r_in,
            "r_out": r_out,
            "fee": fee,
            "in_px": in_px,
            "out_px": out_px,
            "eth_price": gas_eth_price,
            "base_fee": base_fee,
            "capital": capital,
            "state": state,
            "amount_in": amount_in,
            "benign": benign,
            "min_out": int(benign * (1 - intent.slippage)),
            "attacker_gas_usd": config.SANDWICH_GAS
            * (base_fee + intent.tip + config.GWEI)
            / config.E
            * gas_eth_price,
        }

    def assert_current(self, report: RiskReport, intent: SwapIntent | None = None):
        if intent is not None and asdict(intent) != asdict(report.intent):
            raise ValueError("Risk report belongs to a different intent")
        if self.context(report.intent)["state"] != report.extra.get("state"):
            raise RuntimeError("Risk report is stale; request a new decision")

    # ---------------------------------------------------------- simulator
    def _sim_bundle(
        self, intent: SwapIntent, ctx: dict, f: int, min_out: int
    ) -> tuple[int, int, bool]:
        """Run [front(f), victim, back] (or just victim if f == 0) on the shadow fork.
        Returns (victim_out, attacker_profit_in_token_in, victim_ok)."""
        c, d = self.chain, ctx["d"]
        user = account(intent.role).address
        bot = account("sandwich_bot").address
        sw, router = c.c("sandwich"), c.c("router")
        victim = fn_simtx(
            user,
            router.functions.swapExactIn(
                d[intent.amm], d[intent.token_in], ctx["amount_in"], min_out, 2**40
            ),
        )
        txs = [victim]
        if f > 0:
            txs = [
                fn_simtx(
                    bot, sw.functions.frontrun(d[intent.amm], d[intent.token_in], f, 0)
                ),
                victim,
                fn_simtx(
                    bot, sw.functions.backrun(d[intent.amm], d[intent.token_out], 0)
                ),
            ]

        def measure(ch):
            return (
                ch.balance(intent.token_out, user),
                ch.balance(intent.token_in, d["sandwich"]),
            )

        started = time.perf_counter()
        self.stats["shadow_bundles"] += 1
        self.stats["shadow_transactions"] += len(txs)
        try:
            res = self.shadow.simulate(txs, before=measure, after=measure)
        finally:
            self.stats["shadow_ms"] += (time.perf_counter() - started) * 1000
        vi = 1 if f > 0 else 0
        ok = res.receipts[vi]["status"] == 1
        victim_out = res.after[0] - res.before[0]
        profit = res.after[1] - res.before[1]
        return victim_out, profit, ok

    def _exact_bundle(self, ctx: dict, f: int, min_out: int) -> tuple[int, int, bool]:
        r = amm_math.simulate_sandwich(
            ctx["r_in"], ctx["r_out"], ctx["amount_in"], min_out, f, ctx["fee"]
        )
        return r.victim_out, r.profit, not r.victim_reverts

    def best_attack(
        self, intent: SwapIntent, ctx: dict, min_out: int, mode: str
    ) -> dict:
        """Profit-maximising front-run subject to the victim not reverting."""
        bundle = (
            (lambda f: self._sim_bundle(intent, ctx, f, min_out))
            if mode == "sim"
            else (lambda f: self._exact_bundle(ctx, f, min_out))
        )
        f_bound = amm_math.max_frontrun_without_revert(
            ctx["r_in"], ctx["r_out"], ctx["amount_in"], min_out, ctx["fee"]
        )
        f_bound = min(f_bound, ctx["capital"])
        evals = {"n": 0}
        cache: dict[int, tuple] = {}

        def profit(f: float) -> float:
            f = int(f)
            if f not in cache:
                evals["n"] += 1
                cache[f] = bundle(f)
            vo, p, ok = cache[f]
            return p if ok else -float("inf")

        if f_bound <= 0:
            return {"frontrun": 0, "profit": 0, "victim_out": None, "evals": 0}
        iters = self.iters if mode == "sim" else 50
        f_star, p = amm_math.golden_section_max(profit, 0, f_bound, iters=iters)
        f_star = int(f_star)
        profit(f_star)
        vo, p, ok = cache[f_star]
        return {
            "frontrun": f_star,
            "profit": p,
            "victim_out": vo if ok else 0,
            "evals": evals["n"],
        }

    # ---------------------------------------------------------- leakage
    def hint_harm(self, intent: SwapIntent, ctx: dict) -> float:
        """Attacker sees pool + direction only. It picks one front-run size maximising *expected* profit over a
        prior of trade sizes/slippages; the realised harm on this victim follows (a revert costs a retry).
        """
        rng = random.Random(7)
        prior = []
        for _ in range(60):
            v_usd = 10 ** rng.uniform(0.7, 4.3)
            v = int(v_usd / ctx["in_px"] * config.E)
            b = amm_math.amount_out(v, ctx["r_in"], ctx["r_out"], ctx["fee"])
            prior.append((v, int(b * (1 - rng.choice([0.005, 0.01, 0.02, 0.03])))))
        cost_tok = ctx["attacker_gas_usd"] / ctx["in_px"] * config.E

        def expected(f: float) -> float:
            f = int(f)
            return (
                sum(
                    amm_math.simulate_sandwich(
                        ctx["r_in"], ctx["r_out"], v, mo, f, ctx["fee"]
                    ).profit
                    for v, mo in prior
                )
                / len(prior)
                - cost_tok
            )

        f_star, ep = amm_math.golden_section_max(
            expected, 0, ctx["r_in"] // 20, iters=30
        )
        if ep <= 0:
            return 0.0
        r = amm_math.simulate_sandwich(
            ctx["r_in"],
            ctx["r_out"],
            ctx["amount_in"],
            ctx["min_out"],
            int(f_star),
            ctx["fee"],
        )
        if r.victim_reverts:
            return config.RETRY_COST + self.chain.gas_cost_usd(
                config.SWAP_GAS, intent.tip
            )
        return r.harm / config.E * ctx["out_px"]

    # -------------------------------------------------------------- main
    def assess(
        self,
        intent: SwapIntent,
        mode: str = "sim",
        leakage_levels: bool = True,
        min_out: int | None = None,
    ) -> RiskReport:
        if mode not in ("sim", "fast", "hybrid"):
            raise ValueError("Risk mode must be sim, hybrid or fast")
        if mode == "hybrid":
            from .hybrid import assess_hybrid

            return assess_hybrid(self, intent, min_out=min_out)
        if mode == "sim" and self.shadow is None:
            raise RuntimeError(
                "Shadow-fork verification is unavailable; no simulated decision issued"
            )
        t0 = time.perf_counter()
        if mode == "sim":
            self.shadow.sync()
        ctx = self.context(intent)
        if min_out is not None:
            if not 0 <= min_out <= ctx["benign"]:
                raise ValueError("Output bound exceeds the benign quote")
            ctx["min_out"] = min_out
        if mode == "sim":
            out_benign, _, ok = self._sim_bundle(intent, ctx, 0, 0)
            if not ok:
                raise RuntimeError(
                    "Benign swap failed in shadow simulation; no executable recommendation"
                )
        else:
            out_benign = ctx["benign"]
        atk = self.best_attack(intent, ctx, ctx["min_out"], mode)
        profit_usd = atk["profit"] / config.E * ctx["in_px"]
        profitable = atk["frontrun"] > 0 and profit_usd > ctx["attacker_gas_usd"]
        out_attacked = atk["victim_out"] if profitable else out_benign
        harm_usd = max(0.0, (out_benign - out_attacked) / config.E * ctx["out_px"])

        cf = amm_math.closed_form_sandwich(
            ctx["r_in"] / config.E,
            ctx["r_out"] / config.E,
            intent.amount,
            1 - ctx["min_out"] / max(ctx["benign"], 1),
            ctx["fee"],
        )
        cf_profit_usd = cf["profit"] * ctx["in_px"]
        cf_harm = (
            cf["harm"] * ctx["out_px"]
            if cf_profit_usd > ctx["attacker_gas_usd"]
            else 0.0
        )
        raw_harm = max(
            0.0,
            (out_benign - (atk["victim_out"] or out_benign)) / config.E * ctx["out_px"],
        )
        cf_raw = cf["harm"] * ctx["out_px"]
        err = abs(raw_harm - cf_raw) / cf_raw * 100 if cf_raw > 1e-9 else 0.0

        hint = self.hint_harm(intent, ctx) if leakage_levels else 0.0
        report = RiskReport(
            intent=intent,
            out_benign=out_benign,
            min_out=ctx["min_out"],
            frontrun=atk["frontrun"],
            attacker_profit_usd=profit_usd,
            attacker_gas_usd=ctx["attacker_gas_usd"],
            attack_profitable=profitable,
            out_attacked=out_attacked,
            harm_public_usd=harm_usd,
            harm_hint_usd=hint,
            harm_private_usd=0.0,
            p_attack=1.0 if profitable else 0.0,
            closed_form_harm_usd=cf_harm,
            closed_form_error_pct=err,
            out_price_usd=ctx["out_px"],
            in_price_usd=ctx["in_px"],
            sim_ms=(time.perf_counter() - t0) * 1000,
            mode=mode,
            evals=atk["evals"],
            extra={
                "ctx_fee": ctx["fee"],
                "r_in": ctx["r_in"],
                "r_out": ctx["r_out"],
                "amount_in": ctx["amount_in"],
                "capital": ctx["capital"],
                "state": ctx["state"],
                "source": "shadow-fork" if mode == "sim" else "analytic",
            },
        )
        self.assert_current(report)
        return report

    def verify_split(self, intent, n):
        """Validate sequential public chunks against bounded rational attacks on the shadow fork."""
        if self.shadow is None:
            raise RuntimeError("Split decision requires the shadow fork")
        self.shadow.sync()
        ctx = self.context(intent)
        ri, ro, capital = ctx["r_in"], ctx["r_out"], ctx["capital"]
        d = ctx["d"]
        txs = []
        sizes = []
        user, bot = account(intent.role).address, account("sandwich_bot").address
        sw, router = self.chain.c("sandwich"), self.chain.c("router")
        # Match execute(): each chunk obtains its limit from the preceding block's state.
        for chunk in split_amounts(ctx["amount_in"], n):
            quote = amm_math.amount_out(chunk, ri, ro, ctx["fee"])
            minimum = int(quote * (1 - intent.slippage))
            attack = amm_math.optimal_sandwich(
                ri, ro, chunk, minimum, ctx["fee"], capital=capital
            )
            profitable = (
                attack.profit / config.E * ctx["in_px"] > ctx["attacker_gas_usd"]
            )
            victim = fn_simtx(
                user,
                router.functions.swapExactIn(
                    d[intent.amm], d[intent.token_in], chunk, minimum, 2**40
                ),
            )
            if profitable:
                txs.extend(
                    [
                        fn_simtx(
                            bot,
                            sw.functions.frontrun(
                                d[intent.amm], d[intent.token_in], attack.frontrun, 0
                            ),
                        ),
                        victim,
                        fn_simtx(
                            bot,
                            sw.functions.backrun(d[intent.amm], d[intent.token_out], 0),
                        ),
                    ]
                )
                sizes.append(3)
                ri += chunk - attack.profit
                ro -= attack.victim_out
                capital += attack.profit
            else:
                txs.append(victim)
                sizes.append(1)
                ri += chunk
                ro -= quote
        before = lambda ch: ch.balance(intent.token_out, user)
        started = time.perf_counter()
        self.stats["shadow_bundles"] += 1
        self.stats["shadow_transactions"] += len(txs)
        try:
            res = self.shadow.simulate(
                txs, before=before, after=before, block_sizes=sizes
            )
        finally:
            self.stats["shadow_ms"] += (time.perf_counter() - started) * 1000
        if not all(r["status"] == 1 for r in res.receipts):
            raise RuntimeError(
                "Split verification reverted; no executable recommendation"
            )
        if self.context(intent)["state"] != ctx["state"]:
            raise RuntimeError("State changed during split verification")
        return max(
            0, (ctx["benign"] - (res.after - res.before)) / config.E * ctx["out_px"]
        )


def split_amounts(amount, n):
    if not isinstance(n, int) or not 2 <= n <= 5 or amount < n:
        raise ValueError("Invalid split size")
    chunk, remainder = divmod(amount, n)
    return [chunk + (1 if i < remainder else 0) for i in range(n)]


def revert_probability(
    slippage_room: float, blocks: int = 1, sigma: float = config.SIGMA_PER_BLOCK
) -> float:
    """P(an adverse benign price move larger than the slippage room before inclusion) — one-sided normal."""
    s = sigma * math.sqrt(max(blocks, 1))
    return 0.5 * math.erfc(slippage_room / (s * math.sqrt(2)))
