"""Versioned, reproducible analytical experiments. No chain or model pickle is required.

The saved inputs contain reserves, fees and a seeded benign price move. These are exact integer AMM
experiments with estimated gas, not mainnet measurements or EVM execution receipts.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import os
import tempfile
from dataclasses import asdict, dataclass

from .. import config
from ..defense import amm_math
from .routing import PoolView, quote_routes

REPLAY_DIR = config.DATA_DIR / "replays"
MODEL_VERSION = "integer-amm-v1"


@dataclass(frozen=True)
class Scenario:
    amount_usdc: float = 10000
    liquidity_scale: float = 1
    slippage: float = .02
    attacker_capital_usdc: float = 50000
    gas_gwei: float = 2
    guardian_delay_blocks: int = 0
    attacker_tip_gwei: float = 1
    guardian_tip_gwei: float = 2
    seed: int = 42

    def __post_init__(self):
        ranges = {"amount_usdc": (1, 100000), "liquidity_scale": (.05, 20), "slippage": (.00001, .2),
                  "attacker_capital_usdc": (0, 5000000), "gas_gwei": (.01, 200),
                  "attacker_tip_gwei": (0, 200), "guardian_tip_gwei": (0, 200)}
        for name, (lo, hi) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"{name} must be finite and in [{lo}, {hi}]")
        for name, lo, hi in (("guardian_delay_blocks", 0, 5), ("seed", 0, 2**32 - 1)):
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")


def run_replay(scenario: Scenario) -> dict:
    s = scenario
    E = config.E
    price = 2000.0
    base = PoolView("ammA", int(100 * E * s.liquidity_scale), int(200000 * E * s.liquidity_scale), 5)
    # Same seeded market move is shared by every strategy in this experiment.
    drift = random.Random(s.seed).gauss(0, .001 * math.sqrt(s.guardian_delay_blocks + 1))
    factor = math.exp(drift / 2)
    pools = [PoolView("ammA", int(base.weth / factor), int(base.usdc * factor), 5),
             PoolView("ammB", int(base.weth / factor), int(base.usdc * 1.03 * factor), 5)]
    size = int(s.amount_usdc * E)
    initial_out = amm_math.amount_out(size, base.usdc, base.weth, base.fee_bps)
    current_out = amm_math.amount_out(size, pools[0].usdc, pools[0].weth, 5)
    swap_gas = config.SWAP_GAS * s.gas_gwei * 1e-9 * price
    attack_gas = config.SANDWICH_GAS * (s.gas_gwei + s.attacker_tip_gwei) * 1e-9 * price
    capital = int(s.attacker_capital_usdc * E)
    rows = []
    safe = amm_math.max_safe_slippage(pools[0].usdc, pools[0].weth, size, 5, attack_gas * E, hi=s.slippage) * .85
    for strategy, slip, private in (("public", s.slippage, False), ("adaptive-slippage", safe, False),
                                    ("private-relay", s.slippage, True)):
        minimum = int(initial_out * (1 - slip))
        attack = amm_math.optimal_sandwich(pools[0].usdc, pools[0].weth, size, minimum, 5, capital=capital)
        profitable = not private and attack.profit / E > attack_gas and not attack.victim_reverts
        reverted = current_out < minimum
        out = 0 if reverted else (attack.victim_out if profitable else current_out)
        harm = attack.harm / E * price if profitable else 0
        fee = config.RELAY_FEE_USD if private else 0
        rows.append({"strategy": strategy, "completed": not reverted, "received_weth": out / E,
                     "mev_loss_usd": harm, "gas_usd": swap_gas, "fee_usd": fee,
                     "total_cost_usd": harm + swap_gas + fee + (config.RETRY_COST if reverted else 0)})
    routing = quote_routes(size, pools, slippage=s.slippage, gas_per_leg_usd=swap_gas,
                           attacker_gas_usd=attack_gas, capital=capital, reference_price=price)
    route = routing["chosen"]
    route_harm = route["predicted_harm_usd"]
    rows.append({"strategy": "mev-router", "completed": True,
                 "received_weth": (route["quoted_out"] / E - route_harm / price),
                 "mev_loss_usd": route_harm, "gas_usd": route["gas_usd"], "fee_usd": 0,
                 "total_cost_usd": route_harm + route["gas_usd"]})
    # End-user comparison uses a common initial reference quote, so route price improvements count.
    for row in rows:
        row["execution_shortfall_usd"] = ((initial_out / E - row["received_weth"]) * price
                                           if row["completed"] else None)
        row["comparison_cost_usd"] = ((row["execution_shortfall_usd"] or 0) + row["gas_usd"] + row["fee_usd"]
                                        + (config.RETRY_COST if not row["completed"] else 0))
    return {"model_version": MODEL_VERSION, "scenario": asdict(s), "seeded_price_move": drift,
            "rows": rows, "routing": routing,
            "guardian": {"can_precede_attack": s.guardian_delay_blocks == 0 and s.guardian_tip_gwei > s.attacker_tip_gwei,
                         "assumption": "public attack visible before sealing; fee-ordered local builder; configured pause fee"},
            "scope": "analytical integer-AMM replay; ideal private relay; gas estimates; router requotes at inclusion",
            "best_completed": min((r for r in rows if r["completed"]), key=lambda r: r["comparison_cost_usd"])["strategy"]}


def save_replay(scenario: Scenario, directory=None) -> dict:
    root = directory or REPLAY_DIR
    root.mkdir(parents=True, exist_ok=True)
    payload = {"model_version": MODEL_VERSION, "scenario": asdict(scenario)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    replay_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
    path = root / f"{replay_id}.json"
    if not path.exists():
        with tempfile.NamedTemporaryFile(mode="w", dir=root, suffix=".tmp", encoding="utf-8", delete=False) as file:
            file.write(raw)
            temporary = file.name
        os.replace(temporary, path)
    return {"id": replay_id, **payload}


def load_replay(replay_id: str, directory=None) -> Scenario:
    if not re.fullmatch(r"[0-9a-f]{20}", replay_id):
        raise ValueError("invalid replay id")
    payload = json.loads(((directory or REPLAY_DIR) / f"{replay_id}.json").read_text(encoding="utf-8"))
    if payload.get("model_version") != MODEL_VERSION:
        raise ValueError("unsupported replay model version")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if hashlib.sha256(raw.encode()).hexdigest()[:20] != replay_id:
        raise ValueError("replay content does not match its id")
    try:
        return Scenario(**payload["scenario"])
    except (TypeError, KeyError) as exc:
        raise ValueError("invalid saved scenario") from exc


def list_replays(directory=None) -> list[dict]:
    root = directory or REPLAY_DIR
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.glob("*.json"))[-100:]:
        try:
            scenario = load_replay(path.stem, root)
            rows.append({"id": path.stem, "scenario": asdict(scenario)})
        except (ValueError, OSError):
            continue  # A damaged record does not hide other saved experiments.
    return rows
