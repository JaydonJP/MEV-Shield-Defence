"""Paired decisions and actual attacked executions from identical local EVM state."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import time

import numpy as np

from . import config, store
from .chain import BlockBuilder
from .defense import (
    DefenseSelector,
    HarmPredictor,
    HybridPolicy,
    RiskEngine,
    SwapIntent,
    execute,
)
from .extensions.service import isolated_world
from .harness.sandwich_bot import SandwichBot


def source_fingerprint():
    digest = hashlib.sha256()
    for relative in (
        "defense/risk.py",
        "defense/selector.py",
        "defense/hybrid.py",
        "chain/shadow.py",
        "hybrid_benchmark.py",
        "config.py",
        "ml/model.py",
        "ml/artifacts/market-risk.json",
    ):
        path = config.ROOT / "mevshield" / relative
        digest.update(relative.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def workload(quick=True):
    cases = [
        ("small", 10, 0.02, "USDC", "ammA"),
        ("medium", 1000, 0.02, "USDC", "ammA"),
        ("tight", 5000, 0.005, "USDC", "ammA"),
        ("large", 10000, 0.02, "USDC", "ammA"),
        ("high-value", 25000, 0.02, "USDC", "ammA"),
        ("outside-support", 100000, 0.02, "USDC", "ammA"),
        ("reverse", 0.5, 0.01, "WETH", "ammA"),
        ("second-pool", 5000, 0.02, "USDC", "ammB"),
    ]
    if not quick:
        cases += [
            (f"size-{n}-slip-{s}", n, s, "USDC", "ammA")
            for n in (100, 250, 500, 2500, 7500, 15000)
            for s in (0.005, 0.02)
        ]
    return [
        (name, SwapIntent(amount, token_in=token, amm=pool, slippage=slip))
        for name, amount, slip, token, pool in cases
    ]


def summarize(rows, harm_margin_usd=1):
    identities = [(r["case"], r["repeat"], r["mode"]) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate paired benchmark rows")
    modes = {}
    for mode in ("sim", "hybrid", "fast", "public"):
        group = [r for r in rows if r["mode"] == mode]
        modes[mode] = {
            "n": len(group),
            "decision_p50_ms": float(np.median([r["decision_ms"] for r in group])),
            "decision_p95_ms": float(
                np.quantile([r["decision_ms"] for r in group], 0.95)
            ),
            "decision_total_ms": sum(r["decision_ms"] for r in group),
            "client_cpu_total_ms": sum(r["cpu_ms"] for r in group),
            "shadow_bundles": sum(r["shadow_bundles"] for r in group),
            "shadow_transactions": sum(r["shadow_transactions"] for r in group),
            "realized_harm_usd": sum(r["harm_usd"] for r in group),
            "realized_cost_usd": sum(r["realized_cost_usd"] for r in group),
            "reverts": sum(r["reverted"] for r in group),
        }
    paired = []
    for key in sorted({(r["case"], r["repeat"]) for r in rows}):
        group = {r["mode"]: r for r in rows if (r["case"], r["repeat"]) == key}
        base, hybrid = group["sim"], group["hybrid"]
        if any(r["state_hash"] != base["state_hash"] for r in group.values()):
            raise ValueError("Paired modes did not start at identical state")
        paired.append(
            {
                "case": key[0],
                "repeat": key[1],
                "same_defense": base["defense"] == hybrid["defense"],
                "harm_delta_usd": hybrid["harm_usd"] - base["harm_usd"],
                "cost_delta_usd": hybrid["realized_cost_usd"]
                - base["realized_cost_usd"],
                "extra_revert": bool(hybrid["reverted"] and not base["reverted"]),
            }
        )
    sim, hybrid = modes["sim"], modes["hybrid"]
    return {
        "modes": modes,
        "paired": paired,
        "recommendation_agreement": sum(r["same_defense"] for r in paired)
        / len(paired),
        "hybrid_latency_reduction_pct": 100
        * (1 - hybrid["decision_total_ms"] / sim["decision_total_ms"]),
        "hybrid_shadow_bundle_reduction_pct": 100
        * (1 - hybrid["shadow_bundles"] / max(1, sim["shadow_bundles"])),
        "hybrid_ml_only_decisions": sum(
            r["mode"] == "hybrid" and r["shadow_bundles"] == 0 for r in rows
        ),
        "hybrid_risk_fallbacks": sum(
            r["mode"] == "hybrid" and bool(r["decision"].get("fallback_reasons"))
            for r in rows
        ),
        "max_harm_delta_usd": max(r["harm_delta_usd"] for r in paired),
        "harm_margin_usd": harm_margin_usd,
        "worse_protection_cases": sum(
            r["harm_delta_usd"] > harm_margin_usd or r["extra_revert"] for r in paired
        ),
        "comparable_on_sample": all(
            r["harm_delta_usd"] <= harm_margin_usd and not r["extra_revert"]
            for r in paired
        ),
        "interpretation": "Descriptive local paired sample; not a statistical non-inferiority proof or mainnet safety guarantee",
    }


def run_comparison(chain, shadow, quick=True, repeats=1, log=print, policy=None):
    if not 1 <= repeats <= 5:
        raise ValueError("Use one to five repeats")
    if chain.txpool():
        raise RuntimeError("Finish pending transactions before benchmarking")
    if store.query("SELECT COUNT(*) AS n FROM relay_queue WHERE sent=0")[0]["n"]:
        raise RuntimeError("Private relay has pending transactions")
    if store.is_alive("guardian"):
        raise RuntimeError("Stop the external Guardian before this isolated benchmark")
    started = time.perf_counter()
    model_started = time.perf_counter()
    model = HarmPredictor.load()
    model_load_ms = (time.perf_counter() - model_started) * 1000
    warm_started = time.perf_counter()
    # One explicit warm-up, outside all timed decisions. No per-case fitted state.
    model.predict(1000, 200000, 0.02, 5, 2, eth_price_usd=2000, domain="lab")
    warmup_ms = (time.perf_counter() - warm_started) * 1000
    policy = policy or HybridPolicy()
    rows = []
    cases = workload(quick)
    for repeat in range(repeats):
        for index, (name, intent) in enumerate(cases):
            modes = (
                ("sim", "hybrid", "fast", "public")
                if (index + repeat) % 2 == 0
                else ("hybrid", "sim", "fast", "public")
            )
            for mode in modes:
                with isolated_world(chain, shadow):
                    shadow.sync()
                    state_hash = chain.w3.eth.get_block("latest")["hash"].to_0x_hex()
                    engine = RiskEngine(chain, shadow, predictor=model, policy=policy)
                    selector = DefenseSelector(chain, engine)
                    if mode == "public":
                        selection = None
                        decision = {}
                        defense = "public"
                    else:
                        selection = selector.evaluate(intent, mode=mode)
                        decision = selection.extra
                        defense = selection.best.name
                    # Searcher validation/execution time is outside decision timing in every mode.
                    bot = SandwichBot(chain, shadow)
                    builder = BlockBuilder(chain, [bot], external_window=0)
                    price = chain.eth_price()
                    execution_started = time.perf_counter()
                    result = (
                        selector.execute(builder, selection, log=lambda *a: None)
                        if selection
                        else execute(
                            chain, builder, intent, "public", log=lambda *a: None
                        )
                    )
                    receipts = [chain.receipt(h) for h in result.tx_hashes]
                    gas = (
                        sum(r["gasUsed"] * r["effectiveGasPrice"] for r in receipts)
                        / config.E
                        * price
                    )
                    fee = config.RELAY_FEE_USD if defense == "private-relay" else 0
                    latency = max(0, len(result.blocks) - 1)
                    cost = (
                        result.harm_usd
                        + config.LAMBDA_GAS * (gas + fee)
                        + config.LAMBDA_LATENCY_PER_BLOCK * latency
                        + config.RETRY_COST * result.reverted
                    )
                    rows.append(
                        {
                            "case": name,
                            "repeat": repeat,
                            "intent": asdict(intent),
                            "mode": mode,
                            "state_hash": state_hash,
                            "defense": defense,
                            "decision_ms": decision.get("decision_ms", 0),
                            "cpu_ms": decision.get("cpu_ms", 0),
                            "shadow_bundles": decision.get("shadow_bundles", 0),
                            "shadow_transactions": decision.get(
                                "shadow_transactions", 0
                            ),
                            "decision": decision,
                            "harm_usd": result.harm_usd,
                            "reverted": result.reverted,
                            "sandwiched": result.sandwiched,
                            "gas_usd": gas,
                            "relay_fee_usd": fee,
                            "latency_blocks": latency,
                            "realized_cost_usd": cost,
                            "predicted_cost_usd": (
                                selection.best.J if selection else None
                            ),
                            "execution_ms": (time.perf_counter() - execution_started)
                            * 1000,
                            "receipts": [
                                {
                                    "hash": h,
                                    "status": r["status"],
                                    "gas_used": r["gasUsed"],
                                    "block": r["blockNumber"],
                                }
                                for h, r in zip(result.tx_hashes, receipts)
                            ],
                        }
                    )
                    log(
                        f"{name:16} {mode:6} → {defense:18} harm=${result.harm_usd:.4f} decision={decision.get('decision_ms',0):.0f}ms bundles={decision.get('shadow_bundles',0)}"
                    )
    report = {
        "status": "completed",
        "schema": "hybrid-selection-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Known local Anvil contracts; paired adversarial EVM executions, receipts reverted after measurement",
        "source_fingerprint": source_fingerprint(),
        "policy": asdict(policy),
        "quick": quick,
        "repeats": repeats,
        "model_load_ms": model_load_ms,
        "model_warmup_ms": warmup_ms,
        "total_ms": (time.perf_counter() - started) * 1000,
        "timing_scope": "Warm end-to-end selection including reads, math, ML and required verification; excludes execution. Client CPU excludes Anvil CPU; shadow counts are workload proxies, not dollars.",
        "summary": summarize(rows),
        "rows": rows,
    }
    output = config.DATA_DIR / "hybrid-benchmark.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
