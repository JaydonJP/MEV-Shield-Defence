"""DemoEngine — drives the live devnet and streams structured events to the web UI.

One engine instance wraps the whole system (chain, shadow fork, Risk Engine, Defense Selector, sandwich
bot, Guardian + Responder, ML predictor). Each public method performs one scenario and emits a sequence of
events (`phase`, `log`, `mempool`, `block`, `prediction`, `alert`, `result`) as it runs, so the browser
sees the attack unfold and the defense respond in real time. All methods are synchronous (web3 is blocking)
and are run in a worker thread by the server; `emit` hands each event to the server's broadcaster.
"""
from __future__ import annotations

import time
import threading
from typing import Callable
from web3.logs import DISCARD

from .. import config, store
from ..chain import BlockBuilder, Chain, ShadowFork, account, devnet, deploy
from ..defense import DefenseSelector, HarmPredictor, RiskEngine, SwapIntent, execute
from ..guardian import Guardian
from ..harness.attacks import plan_arbitrage, submit_arbitrage, submit_oracle_attack, submit_governance_attack
from ..harness.sandwich_bot import SandwichBot, decode_swap

PACE = 0.45  # seconds between narrative steps, so the UI reads as a sequence rather than a flash


class DemoEngine:
    def __init__(self, emit: Callable[[dict], None] | None = None, pace: float = PACE):
        self._emit = emit or (lambda e: None)
        self.pace = pace
        self.chain: Chain | None = None
        self.shadow: ShadowFork | None = None
        self.model: HarmPredictor | None = None
        self._model_stamp = None
        self._busy = False
        self._action_lock = threading.Lock()
        self.safety = None

    # ------------------------------------------------------------------ wiring
    def set_emitter(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit

    def emit(self, kind: str, **data) -> None:
        self._emit({"kind": kind, "ts": time.time(), **data})

    def log(self, text: str, level: str = "info") -> None:
        self.emit("log", level=level, text=text)

    def _pace(self, mult: float = 1.0) -> None:
        if self.pace:
            time.sleep(self.pace * mult)

    def connect(self) -> bool:
        """Attach to the running devnet. Returns False if it is not up."""
        s = devnet.status()
        if not (s["main"]["alive"] and s["shadow"]["alive"]):
            return False
        if self.chain is None:
            self.chain = Chain()
            self.shadow = ShadowFork(self.chain)
            from ..defense.safety import SafetyReports
            self.safety = SafetyReports(self.chain, self.shadow)
        from ..ml.model import BUNDLED
        stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in (config.MODEL_FILE, BUNDLED))
        if (self.model is None or self._model_stamp != stamp) and HarmPredictor.available():
            self.model = HarmPredictor.load()
            self._model_stamp = stamp
        return True

    # ------------------------------------------------------------------- state
    def status(self) -> dict:
        s = devnet.status()
        up = s["main"]["alive"] and s["shadow"]["alive"]
        out = {"main": s["main"]["alive"], "shadow": s["shadow"]["alive"],
               "model": HarmPredictor.available(), "ready": up, "busy": self._busy}
        if up and self.connect():
            c = self.chain
            d = c.deployments["contracts"]
            r0, r1 = c.reserves("ammA")
            out.update({
                "block": c.block_number,
                "spot": round(r1 / r0, 2),
                "twap": round(c.c("oracle").functions.twap().call() / config.E, 2),
                "pools": {
                    "ammA": _pool(c, "ammA"), "ammB": _pool(c, "ammB")},
                "lending": {
                    "vulnerable": round(c.c("vulnerable").functions.liquidity().call() / config.E, 2),
                    "safe": round(c.c("safe").functions.liquidity().call() / config.E, 2)},
                "vulnerable_paused": c.c("vulnerable").functions.paused().call(),
            })
            if self.model:
                out["model_metrics"] = self.model.metrics_dict()
        return out

    def emit_status(self) -> None:
        self.emit("status", **self.status())

    def _liquidity_usd(self) -> float:
        r0, r1 = self.chain.reserves("ammA")
        return r1 / config.E

    # --------------------------------------------------------------- utilities
    def _classify(self, tx) -> str:
        fn = tx.fn or ""
        if "frontrun" in fn:
            return "frontrun"
        if "backrun" in fn:
            return "backrun"
        if "pause" in fn:
            return "defense"
        if "oracleAttacker" in fn or "govAttacker" in fn:
            return "attack"
        if "flashArb" in fn:
            return "arbitrage"
        if "swapExactIn" in fn or "reveal" in fn:
            return "victim"
        if "commit" in fn:
            return "defense"
        return "other"

    def _emit_block(self, blk) -> None:
        txs = [{"pos": i, "hash": t.hash, "role": t.sender_role or "user", "fn": t.fn,
                "tip": round(t.tip_gwei, 2), "status": t.status, "labels": t.labels, "private": t.private,
                "kind": self._classify(t)} for i, t in enumerate(blk.txs)]
        self.emit("block", number=blk.number, txs=txs)
        # Receipt events expose intra-block reserve moves, including a sandwich's temporary price spike.
        twap = self.chain.c("oracle").functions.twap().call(block_identifier=blk.number) / config.E
        addresses = self.chain.deployments["contracts"]
        for tx in txs:
            receipt = self.chain.receipt(tx["hash"])
            price_steps, swaps = [], []
            for pool in ("ammA", "ammB"):
                contract = self.chain.c(pool)
                for event in contract.events.Sync().process_receipt(receipt, errors=DISCARD):
                    if event["address"].lower() != addresses[pool].lower():
                        continue
                    args = event["args"]
                    if args["reserve0"]:
                        price_steps.append({"pool": pool, "price": round(args["reserve1"] / args["reserve0"], 4),
                                            "log_index": event["logIndex"]})
                for event in contract.events.Swap().process_receipt(receipt, errors=DISCARD):
                    if event["address"].lower() != addresses[pool].lower():
                        continue
                    args = event["args"]
                    usdc_in = args["tokenIn"].lower() == addresses["USDC"].lower()
                    swaps.append({"pool": pool, "amount_in": round(args["amountIn"] / config.E, 6),
                                  "amount_out": round(args["amountOut"] / config.E, 6),
                                  "token_in": "USDC" if usdc_in else "WETH",
                                  "token_out": "WETH" if usdc_in else "USDC", "log_index": event["logIndex"]})
            self.emit("execution", block=blk.number, tx=tx, twap=round(twap, 4),
                      price_steps=sorted(price_steps, key=lambda e: e["log_index"]),
                      swaps=sorted(swaps, key=lambda e: e["log_index"]))

    def _ml_label(self, amount_usd: float) -> dict:
        if not self.model:
            return {}
        r = self._risk_report(amount_usd, config.DEFAULT_SLIPPAGE)
        return {"risk_harm": None if r["harm_usd"] is None else round(r["harm_usd"], 2),
                "p_attack": r["p_attack"], "risk_source": r["source"], "risk_status": r["status"],
                "risk_interval": r.get("interval_usd"), "risk_reasons": r.get("reasons", [])}

    def _risk_report(self, amount, slippage):
        def simulate():
            report = RiskEngine(self.chain, self.shadow).assess(
                SwapIntent(amount, slippage=slippage), mode="sim", leakage_levels=False)
            return {"harm_usd": report.harm_public_usd, "p_attack": report.p_attack}
        if self.model is None:
            try:
                return {**simulate(), "source": "simulator", "status": "simulated", "accepted": True,
                        "interval_usd": None, "reasons": ["ML artifact unavailable; used shadow fork"]}
            except Exception:
                return {"harm_usd": None, "p_attack": None, "source": "none", "status": "unknown",
                        "accepted": False, "reasons": ["Neither model nor simulator is available"]}
        from ..ml.model import MarketRiskPredictor
        options = {}
        if isinstance(self.model, MarketRiskPredictor):
            r0, r1 = self.chain.reserves("ammA")
            options = {"eth_price_usd": r1 / r0, "domain": "lab"}
        return self.model.predict_with_fallback(amount, self._liquidity_usd(), slippage,
            config.POOL_FEE_BPS, (self.chain.base_fee() + config.DEFAULT_TIP + config.GWEI) / config.GWEI,
            simulator=simulate, **options)

    def risk_preview(self, amount, slippage):
        with self._guard():
            return self._risk_report(amount, slippage)

    def safety_preview(self, parameters):
        with self._guard():
            return self.safety.prepare(**parameters)

    def safety_execute(self, report_id, binding):
        with self._guard():
            result = self.safety.execute(report_id, binding)
            self.emit_status()
            return result

    # ------------------------------------------------------------------- reset
    def reset(self) -> None:
        if not self.connect():
            raise RuntimeError("Local lab is offline. Run mevshield start to launch it.")
        deploy.reset(self.chain)
        self.shadow._forked_at = None
        store.clear_runtime()
        self.emit("reset")
        self.log("Devnet reset to the clean deployed state.", "info")
        self.emit_status()

    # =================================================================== swaps
    def run_sandwich(self, amount: float, protect: bool, defense_choice: str = "auto", mode: str = "hybrid") -> dict:
        """User swap, with the sandwich bot lurking. protect=False shows the attack; True shows the defense."""
        with self._guard():
            c, sh = self.chain, self.shadow
            bot = SandwichBot(c, sh, log=lambda m: self.log(m.replace("[bot] ", ""), "warn"))
            builder = BlockBuilder(c, [bot], log=lambda *a: None)
            intent = SwapIntent(amount)
            self.emit("phase", name="intercept", label="Transaction intercepted", state="start")
            self.log(f"User wants to swap {amount:,.0f} USDC → WETH on AMM-A.", "info")
            # Protected swaps perform one shared assessment inside the selector.
            ml = self._ml_label(amount) if not protect else {}
            self.emit("mempool", txs=[{"role": "alice", "kind": "victim",
                                       "amount_usd": amount, "label": "user swap", **ml}])
            if ml and ml["risk_harm"] is not None:
                self.log(f"Risk estimate ({ml['risk_source']}): sandwich harm ${ml['risk_harm']:,.2f}, "
                         f"profitable-attack label={ml['p_attack']:.0%}.", "warn" if ml["p_attack"] > 0.5 else "info")
            elif ml:
                self.log("Risk unknown: the model abstained and simulation was unavailable.", "warn")
            self._pace()

            if protect:
                self.emit("phase", name="predict", label="Counterfactual prediction", state="start")
                sel = DefenseSelector(c, RiskEngine(c, sh, predictor=self.model))
                selection = sel.evaluate(intent, mode=mode)
                self._emit_prediction(amount, selection)
                best = selection.best if defense_choice == "auto" else selection.get(defense_choice)
                self.log(f"Risk source {selection.extra['risk_source']} estimates ${selection.get('public').harm:,.2f} public harm. "
                         f"Selector chooses '{best.name}'.", "info")
                self._pace()
                self.emit("phase", name="prevent", label=f"Prevention — {best.name}", state="start")
                res = sel.execute(builder, selection, choice=defense_choice,
                                  log=lambda m: self.log(m.strip(), "info"))
                defense = best.name
            else:
                self.emit("phase", name="expose", label="Sent to the public mempool", state="start")
                self.log("Swap broadcast with no protection — visible to every searcher.", "warn")
                self._pace()
                res = execute(c, builder, intent, "public", {"slippage": intent.slippage},
                              log=lambda m: self.log(m.strip(), "info"))
                defense = "public"

            for blk in res.blocks:
                self._emit_block(blk)
                self._pace(0.7)

            outcome = "reverted" if res.reverted else "defended" if res.harm_usd < 1 else "attacked"
            if res.sandwiched and res.harm_usd < 1:
                detail = "The bot front-ran, but the tightened bound made its sandwich unprofitable."
            elif res.sandwiched:
                detail = "The bot front-ran the swap, pushed the price, and sold after — the victim overpaid."
            elif defense == "private-relay":
                detail = "The swap went straight into the block; the bot never saw it in the public mempool."
            elif defense == "public":
                detail = "No profitable sandwich existed at this size, so the swap settled normally."
            else:
                detail = "The swap settled with negligible harm."
            self.emit("result", title=("Swap reverted" if res.reverted else f"{'Protected' if protect else 'Unprotected'} swap settled"),
                      harm_usd=round(res.harm_usd, 2), protected=protect, defense=defense,
                      sandwiched=res.sandwiched, outcome="reverted" if res.reverted else outcome,
                      detail="The output bound was not met. No tokens were exchanged; gas was spent." if res.reverted else detail,
                      expected_out=round(res.expected_out / config.E, 6),
                      actual_out=round(res.actual_out / config.E, 6))
            store.add_swap({"tx_hash": res.tx_hashes[0], "user": "alice", "amount": amount, "defense": defense,
                            "expected_out": res.expected_out / config.E, "actual_out": res.actual_out / config.E,
                            "harm_usd": res.harm_usd, "sandwiched": int(res.sandwiched), "block": c.block_number})
            self.log(f"Realised harm: ${res.harm_usd:,.2f}.",
                     "success" if res.harm_usd < 1 else "critical")
            self.emit_status()
            return {"harm_usd": res.harm_usd, "outcome": outcome}

    def predict(self, amount: float, mode: str = "hybrid", verify: bool = False) -> dict:
        with self._guard():
            c, sh = self.chain, self.shadow
            self.emit("phase", name="predict", label="Counterfactual prediction", state="start")
            sel = DefenseSelector(c, RiskEngine(c, sh, predictor=self.model))
            selection = sel.evaluate(SwapIntent(amount), mode=mode, require_verification=verify)
            self._emit_prediction(amount, selection)
            self.log(f"Recommendation for {amount:,.0f} USDC: {selection.best.name}.", "info")
            return {"recommendation": selection.best.name, "decision": selection.extra,
                    "costs": {e.name:e.J for e in selection.evals}}

    def _emit_prediction(self, amount: float, selection) -> None:
        r = selection.report
        rounded = lambda value, digits=2: None if value is None else round(value,digits)
        defenses = [{"name": e.name, "harm": round(e.harm, 2), "gas": round(e.gas + e.fee, 2),
                     "latency": e.latency_blocks, "revert": round(e.revert_risk, 3), "J": round(e.J, 2),
                     "chosen": e.name == selection.best.name,
                     "slippage": e.params.get("slippage")} for e in sorted(selection.evals, key=lambda e: e.J)]
        self.emit("prediction", amount_usd=amount, harm_public=round(r.harm_public_usd, 2),
                  attacker_profit=rounded(r.attacker_profit_usd), p_attack=r.p_attack,
                  harm_hint=rounded(r.harm_hint_usd), closed_form_harm=rounded(r.closed_form_harm_usd),
                  closed_form_error=rounded(r.closed_form_error_pct, 3), sim_ms=round(r.sim_ms, 0),
                  recommendation=selection.best.name, defenses=defenses, decision=selection.extra)

    # ============================================================== protocol
    def run_oracle(self, target: str = "vulnerable", guard: bool = False) -> dict:
        with self._guard():
            c, sh = self.chain, self.shadow
            guardian = Guardian(c, sh, respond=True, log=lambda m: None) if guard else None
            searchers = [guardian] if guardian else []
            liq0 = c.c(target).functions.liquidity().call()
            self.emit("phase", name="attack", label="Flash-loan oracle attack", state="start")
            self.log(f"Attacker targets {target}Lending (liquidity ${liq0 / config.E:,.0f}).", "warn")
            self._pace()
            if guard:
                self.emit("phase", name="detect", label="Guardian analysis", state="start")
                self.log("Guardian replays the pending transaction on the shadow fork…", "info")
            h, plan = submit_oracle_attack(c, target)
            if guardian:
                guardian.on_pending(c, c.txpool())
                for v in guardian.verdicts:
                    self._emit_verdict(v)
                    self._pace()
            blk = BlockBuilder(c, searchers, log=lambda *a: None).build_block()
            self._emit_block(blk)
            self._pace(0.7)
            liq1 = c.c(target).functions.liquidity().call()
            reverted = c.receipt(h)["status"] != 1
            drained = (liq0 - liq1) / config.E
            if reverted:
                outcome, detail = "defended", (
                    "The Guardian paused the market one slot before the attack, so it reverted — funds intact."
                    if guard else "SafeLending rejected the borrow. TWAP pricing, the deviation guard, "
                    "a deposit cooldown and a per-block borrow cap protect this market.")
            else:
                outcome, detail = "attacked", f"The attacker manipulated the spot price and drained ${drained:,.0f}."
            self.emit("result", title=f"Oracle attack on {target}Lending",
                      harm_usd=round(max(0.0, drained), 2), protected=guard or target == "safe",
                      outcome=outcome, detail=detail,
                      liquidity_before=round(liq0 / config.E, 2), liquidity_after=round(liq1 / config.E, 2))
            self.log(detail, "success" if reverted else "critical")
            self.emit_status()
            return {"reverted": reverted, "drained": max(0.0, drained)}

    def run_arbitrage(self) -> dict:
        with self._guard():
            c, sh = self.chain, self.shadow
            plan = plan_arbitrage(c)
            guardian = Guardian(c, sh, respond=False, log=lambda m: None)
            self.emit("phase", name="arbitrage", label="Flash-loan arbitrage", state="start")
            if not plan:
                self.log("No arbitrage opportunity right now.", "info")
                self.emit("result", title="No price gap to trade", outcome="benign", harm_usd=0,
                          detail="The two pool prices have already converged. Reset the lab to try the initial opportunity again.")
                return {}
            self.log(f"Arbitrageur flash-borrows {plan['amount'] / config.E:,.0f} USDC to close a "
                     f"{plan['gap'] * 100:.1f}% price gap between the two pools.", "info")
            self.emit("phase", name="detect", label="Guardian analysis", state="start")
            self._pace()
            h, _ = submit_arbitrage(c)
            guardian.on_pending(c, c.txpool())
            for v in guardian.verdicts:
                self._emit_verdict(v)
            blk = BlockBuilder(c, [guardian], log=lambda *a: None).build_block()
            self._emit_block(blk)
            self._pace(0.7)
            self.emit("result", title="Flash-loan arbitrage", harm_usd=0.0, protected=True, outcome="benign",
                      detail="A flash loan with profit but no protocol loss — the Guardian correctly rules it "
                             "benign and leaves it untouched. Prices converged.")
            self.log("Guardian verdict: BENIGN. Legitimate MEV is not flagged.", "success")
            self.emit_status()
            return {"benign": True}

    def run_governance(self, target: str = "vulnerable") -> dict:
        with self._guard():
            c = self.chain
            dao = "vulnerableDAO" if target == "vulnerable" else "snapshotDAO"
            t0 = c.balance("USDC", c.deployments["contracts"][dao])
            self.emit("phase", name="attack", label="Flash-loan governance attack", state="start")
            self.log(f"Attacker flash-borrows governance tokens to seize {dao} (treasury ${t0 / config.E:,.0f}).",
                     "warn")
            self._pace()
            h, _ = submit_governance_attack(c, dao)
            blk = BlockBuilder(c, [], log=lambda *a: None).build_block()
            self._emit_block(blk)
            self._pace(0.7)
            t1 = c.balance("USDC", c.deployments["contracts"][dao])
            reverted = c.receipt(h)["status"] != 1
            detail = ("Snapshot voting counts balances from before the proposal, so flash-borrowed tokens have "
                      "no votes — the attack reverted.") if reverted else \
                f"The DAO treasury was drained of ${(t0 - t1) / config.E:,.0f}."
            self.emit("result", title=f"Governance attack on {dao}", harm_usd=round((t0 - t1) / config.E, 2),
                      protected=reverted, outcome="defended" if reverted else "attacked", detail=detail)
            self.log(detail, "success" if reverted else "critical")
            self.emit_status()
            return {"reverted": reverted}

    def _emit_verdict(self, v) -> None:
        self.emit("alert", label=v.label, severity=v.severity, sender_role=v.sender_role or "user",
                  reasons=v.reasons, score=v.score, action="",
                  features={"flash_loan": v.features.flash_loan,
                            "flash_amount": round(v.features.flash_amount, 1),
                            "price_deviation": round(v.features.max_price_deviation, 3),
                            "protocol_loss": round(v.features.protocol_loss_usd, 1),
                            "sender_profit": round(v.features.sender_profit_usd, 1)})
        lvl = {"CRITICAL": "critical", "SUSPICIOUS": "warn", "BENIGN": "success"}[v.label]
        self.log(f"Guardian: {v.label} — {'; '.join(v.reasons) or 'ordinary activity'}", lvl)

    # ------------------------------------------------------------------- guard
    def _guard(self):
        engine = self

        class _Ctx:
            def __enter__(self_):
                if not engine._action_lock.acquire(blocking=False):
                    raise RuntimeError("another devnet action is running")
                try:
                    if not engine.connect():
                        raise RuntimeError("devnet is not running")
                except Exception:
                    engine._action_lock.release()
                    raise
                engine._busy = True

            def __exit__(self_, *a):
                engine._busy = False
                engine._action_lock.release()

        return _Ctx()

    def run_extension(self, feature: str, amount: float | None = None) -> dict:
        from ..extensions.service import run_feature, run_suite
        with self._guard():
            self.log(f"Running {feature} extension on the local devnet.")
            result = (run_suite(self.chain, self.shadow) if feature == "all" else
                      run_feature(self.chain, self.shadow, feature, amount))
            self.emit("extension", feature=feature, result=result)
            self.emit_status()
            return result


def _pool(c: Chain, key: str) -> dict:
    r0, r1 = c.reserves(key)
    return {"weth": round(r0 / config.E, 4), "usdc": round(r1 / config.E, 2), "price": round(r1 / r0, 2)}
