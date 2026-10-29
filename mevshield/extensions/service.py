"""One entry point shared by the CLI, web dashboard and integration tests."""
from __future__ import annotations

from contextlib import contextmanager

from .. import config, store
from .coordinated import run_coordinated
from .routing import execute_route
from .settlement import run_batch, run_cashback, run_firewall

FEATURES = ("routing", "cashback", "batch", "firewall", "coordinated")


@contextmanager
def isolated_world(chain, shadow):
    """All benchmark scenarios start from one state and restore it even when an experiment fails."""
    if chain.txpool():
        raise RuntimeError("mine or reset existing pending transactions before running experiments")
    snap = chain.snapshot()
    try:
        yield
    finally:
        chain.drop_all_pending()
        if not chain.revert(snap):
            raise RuntimeError("failed to restore experiment snapshot")
        shadow._forked_at = None


def run_feature(chain, shadow, feature: str, amount: float | None = None) -> dict:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")
    if any(k not in chain.deployments["contracts"] for k in ("mevRouter", "cashback", "batch", "firewall")):
        raise RuntimeError("extensions are not deployed; run mevshield down then mevshield up")
    if chain.txpool():
        raise RuntimeError("pending transactions exist; mine or reset them before a feature demo")
    if feature == "routing":
        result = execute_route(chain, shadow, amount if amount is not None else 10000)
    elif feature == "cashback":
        result = run_cashback(chain, amount if amount is not None else 1000)
    elif feature == "batch":
        result = run_batch(chain, amount if amount is not None else 1000)
    elif feature == "firewall":
        result = run_firewall(chain, amount if amount is not None else 8000)
    else:
        result = run_coordinated(chain, shadow, amount if amount is not None else 10000)
    store.save_experiment(f"extension_{feature}", result)
    return {"feature": feature, **result}


def run_suite(chain, shadow) -> dict:
    results = {}
    for name in FEATURES:
        with isolated_world(chain, shadow):
            results[name] = run_feature(chain, shadow, name)
    from .replay import Scenario, run_replay
    results["replay"] = run_replay(Scenario())
    import json
    output = {"scope": "isolated local-devnet experiments; receipts are reverted after each scenario",
              "results": results}
    (config.DATA_DIR / "extensions-results.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output
